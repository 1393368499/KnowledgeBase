import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Base64;
import java.util.HashMap;
import java.util.List;
import java.util.Locale;

import org.apache.poi.hwpf.HWPFDocument;
import org.apache.poi.hwpf.usermodel.Table;
import org.apache.poi.hwpf.usermodel.TableIterator;
import org.apache.poi.hssf.usermodel.HSSFWorkbook;
import org.apache.poi.hslf.usermodel.HSLFSlideShow;
import org.apache.poi.poifs.filesystem.DirectoryEntry;
import org.apache.poi.poifs.filesystem.POIFSFileSystem;
import org.apache.poi.sl.usermodel.GroupShape;
import org.apache.poi.sl.usermodel.PictureShape;
import org.apache.poi.sl.usermodel.Shape;
import org.apache.poi.sl.usermodel.TableShape;
import org.apache.poi.sl.usermodel.TextShape;
import org.apache.poi.ss.usermodel.DataFormatter;
import org.apache.poi.ss.usermodel.Row;
import org.apache.poi.xslf.usermodel.XMLSlideShow;
import org.docx4j.Docx4J;
import org.docx4j.openpackaging.packages.WordprocessingMLPackage;

/** One bounded conversion per process. File paths are supplied by the socket server. */
public final class Convert {
    private final BufferedWriter output;
    private int blocks;

    private Convert(BufferedWriter output) {
        this.output = output;
    }

    public static void main(String[] args) throws Exception {
        Path source = Path.of(args[1]);
        Path target = Path.of(args[2]);
        switch (args[0]) {
            case "pdf" -> renderPdf(source, target);
            case "pptx" -> {
                try (var input = Files.newInputStream(source);
                     var deck = new XMLSlideShow(input);
                     var writer = Files.newBufferedWriter(target, StandardCharsets.UTF_8)) {
                    var converter = new Convert(writer);
                    int page = 0;
                    for (var slide : deck.getSlides()) {
                        converter.slide(slide.getShapes(), ++page);
                    }
                }
            }
            case "doc", "xls", "ppt" -> {
                try (var input = Files.newInputStream(source);
                     var filesystem = new POIFSFileSystem(input);
                     var writer = Files.newBufferedWriter(target, StandardCharsets.UTF_8)) {
                    rejectMacros(filesystem.getRoot(), 0);
                    new Convert(writer).readLegacy(args[0], filesystem);
                }
            }
            default -> throw new IllegalArgumentException("Unsupported operation");
        }
    }

    private static void renderPdf(Path source, Path target) throws Exception {
        var settings = Docx4J.createFOSettings();
        settings.setOpcPackage(WordprocessingMLPackage.load(source.toFile()));
        try (var stream = Files.newOutputStream(target)) {
            Docx4J.toFO(settings, stream, Docx4J.FLAG_EXPORT_PREFER_XSL);
        }
    }

    private static void rejectMacros(DirectoryEntry dir, int depth) throws IOException {
        if (depth > 32) {
            throw new IOException("Too many nested OLE directories");
        }
        for (var entry : dir) {
            String name = entry.getName().toLowerCase(Locale.ROOT);
            if (name.equals("vba") || name.equals("macros") || name.equals("_vba_project_cur")) {
                throw new IOException("Macro storage is not permitted");
            }
            if (entry instanceof DirectoryEntry child) {
                rejectMacros(child, depth + 1);
            }
        }
    }

    private void readLegacy(String kind, POIFSFileSystem filesystem) throws IOException {
        switch (kind) {
            case "doc" -> {
                try (var doc = new HWPFDocument(filesystem)) {
                    var range = doc.getRange();
                    var tables = new HashMap<Integer, Table>();
                    var iterator = new TableIterator(range);
                    while (iterator.hasNext()) {
                        var table = iterator.next();
                        tables.put(table.getStartOffset(), table);
                    }
                    int tableEnd = -1;
                    for (int index = 0; index < range.numParagraphs(); index++) {
                        var paragraph = range.getParagraph(index);
                        var table = tables.get(paragraph.getStartOffset());
                        if (table != null && paragraph.getStartOffset() >= tableEnd) {
                            for (int row = 0; row < table.numRows(); row++) {
                                var cells = table.getRow(row);
                                var values = new ArrayList<String>();
                                for (int cell = 0; cell < cells.numCells(); cell++) {
                                    values.add(cells.getCell(cell).text().strip());
                                }
                                text(0, "table", String.join(" | ", values));
                            }
                            tableEnd = table.getEndOffset();
                        } else if (paragraph.getStartOffset() >= tableEnd) {
                            text(0, "text", paragraph.text());
                        }
                    }
                }
            }
            case "xls" -> {
                try (var book = new HSSFWorkbook(filesystem)) {
                    var formatter = new DataFormatter(Locale.ROOT);
                    formatter.setUseCachedValuesForFormulaCells(true);
                    for (int index = 0; index < book.getNumberOfSheets(); index++) {
                        var sheet = book.getSheetAt(index);
                        text(index + 1, "sheet", sheet.getSheetName());
                        for (Row row : sheet) {
                            var values = new ArrayList<String>();
                            for (int col = 0; col < row.getLastCellNum(); col++) {
                                values.add(formatter.formatCellValue(row.getCell(col)));
                            }
                            if (values.stream().anyMatch(value -> !value.isBlank())) {
                                text(index + 1, "table", String.join(" | ", values));
                            }
                        }
                    }
                }
            }
            case "ppt" -> {
                try (var deck = new HSLFSlideShow(filesystem)) {
                    int page = 0;
                    for (var slide : deck.getSlides()) {
                        slide(slide.getShapes(), ++page);
                    }
                }
            }
            default -> throw new IllegalArgumentException("Unsupported OLE document");
        }
    }

    private void slide(Iterable<? extends Shape<?, ?>> shapes, int page) throws IOException {
        var pictures = new ArrayList<PictureShape<?, ?>>();
        int before = blocks;
        slideText(shapes, page, pictures, 0);
        if (blocks == before) {
            for (var picture : pictures) {
                var data = picture.getPictureData();
                if (data != null && List.of("image/png", "image/jpeg").contains(data.getContentType())) {
                    byte[] bytes = data.getData();
                    if (bytes.length <= 10 * 1024 * 1024) {
                        block(page, "image", bytes);
                    }
                }
            }
        }
    }

    private void slideText(Iterable<? extends Shape<?, ?>> shapes, int page,
                           List<PictureShape<?, ?>> pictures, int depth) throws IOException {
        if (depth > 32) {
            throw new IOException("Too many nested slide groups");
        }
        for (var shape : shapes) {
            if (shape instanceof TableShape<?, ?> table) {
                for (int row = 0; row < table.getNumberOfRows(); row++) {
                    var values = new ArrayList<String>();
                    for (int column = 0; column < table.getNumberOfColumns(); column++) {
                        var cell = table.getCell(row, column);
                        values.add(cell == null ? "" : cell.getText());
                    }
                    text(page, "table", String.join(" | ", values));
                }
            } else if (shape instanceof TextShape<?, ?> text) {
                text(page, "text", text.getText());
            } else if (shape instanceof GroupShape<?, ?> group) {
                slideText(group.getShapes(), page, pictures, depth + 1);
            } else if (shape instanceof PictureShape<?, ?> picture) {
                pictures.add(picture);
            }
        }
    }

    private void text(int page, String kind, String text) throws IOException {
        String cleaned = text.replaceAll("[\\p{Cc}&&[^\\r\\n\\t]]", "").strip();
        if (!cleaned.isEmpty()) {
            block(page, kind, cleaned.getBytes(StandardCharsets.UTF_8));
        }
    }

    private void block(int page, String kind, byte[] data) throws IOException {
        if (++blocks > 20_000) {
            throw new IOException("Too many extracted blocks");
        }
        output.write(page + "\t" + kind + "\t" + Base64.getEncoder().encodeToString(data));
        output.newLine();
    }
}
