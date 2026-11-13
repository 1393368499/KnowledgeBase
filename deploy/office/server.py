"""Socket-only Office converter. Each input gets a fresh, bounded Java process."""

import os
from pathlib import Path
import re
import shutil
import signal
import socketserver
import stat
import subprocess
import tempfile
import time
from urllib.parse import urlsplit
from xml.parsers import expat
from zipfile import BadZipFile, ZipFile

from apps.knowledge.office_wire import HEADER, JOB_TIMEOUT, MAX_INPUT, MAX_OUTPUT, OPERATIONS

SOCKET = Path("/run/kb-office/convert.sock")
HOME = Path("/opt/office")
MODES = {value: key for key, value in OPERATIONS.items()}
XML_LIMIT = 32 * 1024 * 1024
PACKAGE_LIMIT = 512 * 1024 * 1024
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships "
XML_NS = "http://www.w3.org/XML/1998/namespace "
SAFE_FIELDS = {
    "AUTHOR", "CREATEDATE", "DATE", "DOCPROPERTY", "FILENAME", "KEYWORDS",
    "NUMPAGES", "PAGE", "PAGEREF", "REF", "SAVEDATE", "SECTION",
    "SECTIONPAGES", "SEQ", "STYLEREF", "SUBJECT", "TIME", "TITLE", "TOC",
}
LINK_FIELD = re.compile(r'^HYPERLINK\s+(?:"([^"]+)"|([^\s]+))', re.I)


class BadDocument(ValueError):
    pass


def deny(*_):
    raise BadDocument("Active or external document content")


def validate_field(instruction):
    instruction = instruction.strip()
    if not instruction:
        return
    command = instruction.split(maxsplit=1)[0].upper()
    if command in SAFE_FIELDS:
        return
    if command == "HYPERLINK" and (match := LINK_FIELD.match(instruction)):
        target = match.group(1) or match.group(2)
        if target == r"\l" or urlsplit(target).scheme in {"https", "http", "mailto"}:
            return
    deny()


def validate_xml(stream, *, deadline, svg=False):
    parser = expat.ParserCreate(namespace_separator=" ")
    parser.StartDoctypeDeclHandler = deny
    parser.EntityDeclHandler = deny
    parser.ExternalEntityRefHandler = deny
    parser.ProcessingInstructionHandler = deny
    instructions = []
    in_instruction = False

    def start(name, attributes):
        nonlocal in_instruction
        local = name.rsplit(" ", 1)[-1]
        if XML_NS + "base" in attributes or name.startswith("http://www.w3.org/2001/XInclude "):
            deny()
        if name == REL_NS + "Relationship":
            kind = attributes.get("Type", "").rsplit("/", 1)[-1]
            target = urlsplit(attributes.get("Target", ""))
            if kind in {"attachedTemplate", "oleObject", "control", "vbaProject", "externalLink"}:
                deny()
            if attributes.get("TargetMode", "").casefold() == "external":
                if kind != "hyperlink" or target.scheme not in {"https", "http", "mailto"}:
                    deny()
            elif target.scheme or target.netloc:
                deny()
        if local == "fldChar":
            if in_instruction:
                deny()
            kind = next((value for key, value in attributes.items() if key.endswith(" fldCharType")), "")
            if kind == "begin":
                if len(instructions) >= 32:
                    deny()
                instructions.append([])
            elif kind == "separate" and instructions and instructions[-1] is not None:
                validate_field("".join(instructions[-1]))
                instructions[-1] = None
            elif kind == "end" and instructions:
                if (parts := instructions.pop()) is not None:
                    validate_field("".join(parts))
        if local == "instrText":
            if in_instruction or not instructions or instructions[-1] is None:
                deny()
            in_instruction = True
        if local == "fldSimple":
            for key, value in attributes.items():
                if key.endswith(" instr"):
                    validate_field(value)
        if svg and (
            local in {"script", "foreignObject", "style"}
            or any(
                key.rsplit(" ", 1)[-1].lower().startswith("on")
                or (key.rsplit(" ", 1)[-1] in {"href", "src"} and not value.startswith("#"))
                or (re.search(r"url\s*\(", value, re.I) and not re.fullmatch(r"url\(#[\w.:-]+\)", value))
                for key, value in attributes.items()
            )
        ):
            deny()

    def end(name):
        nonlocal in_instruction
        if name.rsplit(" ", 1)[-1] == "instrText":
            in_instruction = False

    def characters(value):
        if in_instruction:
            instructions[-1].append(value)

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.CharacterDataHandler = characters
    while chunk := stream.read(64 * 1024):
        if time.monotonic() >= deadline:
            raise subprocess.TimeoutExpired("Office package validation", JOB_TIMEOUT)
        parser.Parse(chunk, False)
    parser.Parse(b"", True)
    if instructions:
        deny()


def validate_package(source, deadline):
    with ZipFile(source) as package:
        entries = package.infolist()
        if len(entries) > 4096 or sum(entry.file_size for entry in entries) > PACKAGE_LIMIT:
            raise BadDocument("Expanded Office package exceeds limits")
        names = set()
        for entry in entries:
            name = entry.filename.casefold()
            if (
                name in names or "\\" in name or ":" in name or name.startswith("/")
                or any(part in {"..", "."} for part in name.split("/"))
                or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1
                or "vbaproject" in name or "/activex/" in name
            ):
                raise BadDocument("Unsupported Office package entry")
            names.add(name)
            if name.endswith((".xml", ".rels", ".svg")):
                if entry.file_size > XML_LIMIT:
                    raise BadDocument("XML part exceeds limits")
                with package.open(entry) as stream:
                    validate_xml(stream, deadline=deadline, svg=name.endswith(".svg"))


def process_limits():
    import resource

    # Linux container only. Single-threaded parent; this runs between fork and exec.
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT, MAX_OUTPUT))  # pyright: ignore[reportAttributeAccessIssue]
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # pyright: ignore[reportAttributeAccessIssue]


def run_java(mode, source, target, work, deadline):
    command = [
        str(HOME / "java/bin/java"), "-Xmx384m", "-XX:+ExitOnOutOfMemoryError",
        "-XX:ActiveProcessorCount=2", "-Djava.awt.headless=true", "-Dfile.encoding=UTF-8",
        "-Djavax.xml.accessExternalDTD=", "-Djavax.xml.accessExternalSchema=",
        "-Djavax.xml.accessExternalStylesheet=", f"-Djava.io.tmpdir={work}",
        "-cp", f"{HOME}:{HOME}/lib/*", "Convert", mode, str(source), str(target),
    ]
    env = {"HOME": str(work), "XDG_CACHE_HOME": str(work), "LANG": "C.UTF-8"}
    with subprocess.Popen(
        command, cwd=work, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, preexec_fn=process_limits,
    ) as process:
        try:
            code = process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)  # pyright: ignore[reportAttributeAccessIssue]
            process.wait()
            raise
    if code or not target.is_file():
        raise BadDocument("Office engine could not read the input")


class Handler(socketserver.StreamRequestHandler):
    timeout = 12

    def reply(self, status, message):
        self.wfile.write(HEADER.pack(status, len(message)) + message)

    def handle(self):
        header = self.rfile.read(HEADER.size)
        if len(header) != HEADER.size:
            return
        operation, length = HEADER.unpack(header)
        if operation == OPERATIONS["ping"] and length == 0:
            self.reply(0, b"")
            return
        if operation not in MODES or operation == OPERATIONS["ping"] or not 0 < length <= MAX_INPUT:
            self.reply(1, b"invalid request")
            return
        mode = MODES[operation]
        deadline = time.monotonic() + JOB_TIMEOUT
        try:
            with tempfile.TemporaryDirectory(prefix="office-") as work:
                source = Path(work) / ("input.docx" if mode == "pdf" else f"input.{mode}")
                target = Path(work) / "output"
                with source.open("wb") as stream:
                    while length:
                        if time.monotonic() >= deadline:
                            raise subprocess.TimeoutExpired("Office input", JOB_TIMEOUT)
                        remaining = max(0.001, deadline - time.monotonic())
                        self.connection.settimeout(
                            remaining if self.timeout is None else min(self.timeout, remaining)
                        )
                        chunk = self.rfile.read1(min(length, 128 * 1024))
                        if not chunk:
                            raise BadDocument("Incomplete input")
                        stream.write(chunk)
                        length -= len(chunk)
                if mode in {"pdf", "pptx"}:
                    validate_package(source, deadline)
                run_java(mode, source, target, work, deadline)
                size = target.stat().st_size
                if not 0 < size <= MAX_OUTPUT:
                    raise BadDocument("Empty or oversized Office output")
                self.connection.settimeout(self.timeout)
                self.wfile.write(HEADER.pack(0, size))
                with target.open("rb") as stream:
                    shutil.copyfileobj(stream, self.wfile, length=128 * 1024)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except subprocess.TimeoutExpired:
            self.reply(2, b"conversion timeout")
        except (BadZipFile, BadDocument, expat.ExpatError, ValueError):
            self.reply(1, b"invalid or unsupported Office document")
        except OSError:
            self.reply(3, b"converter unavailable")


def main():
    import fcntl

    if not (HOME / "java/bin/java").is_file():
        raise RuntimeError("Office runtime missing")
    os.umask(0o077)
    with SOCKET.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # pyright: ignore[reportAttributeAccessIssue]
        if SOCKET.is_socket():
            SOCKET.unlink()
        with socketserver.UnixStreamServer(str(SOCKET), Handler) as server:  # pyright: ignore[reportAttributeAccessIssue]
            server.serve_forever()


if __name__ == "__main__":
    main()
