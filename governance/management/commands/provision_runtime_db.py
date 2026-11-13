from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from psycopg import sql


class Command(BaseCommand):
    help = "Provision the dedicated PostgreSQL runtime login after schema migration."

    def add_arguments(self, parser):
        parser.add_argument("password_file", type=Path)

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            raise CommandError("PostgreSQL is required")
        password = options["password_file"].read_text(encoding="utf-8").strip()
        if len(password) < 24:
            raise CommandError("Runtime database secret must contain at least 24 characters")

        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(774205503)")
            cursor.execute("""
                SELECT rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls
                    OR EXISTS (SELECT FROM pg_auth_members WHERE member = pg_roles.oid)
                FROM pg_roles WHERE rolname = 'kb_runtime'
            """)
            existing = cursor.fetchone()
            if existing and existing[0]:
                raise CommandError("Existing kb_runtime role has elevated privileges or memberships")
            if existing is None:
                cursor.execute("CREATE ROLE kb_runtime LOGIN")
            cursor.execute(sql.SQL("ALTER ROLE kb_runtime LOGIN PASSWORD {}").format(sql.Literal(password)))
            cursor.execute("GRANT USAGE ON SCHEMA public TO kb_runtime")
            cursor.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO kb_runtime")
            cursor.execute("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO kb_runtime")
            cursor.execute("REVOKE INSERT, UPDATE, DELETE ON django_migrations FROM kb_runtime")
            cursor.execute("REVOKE UPDATE, DELETE ON audit_auditevent FROM kb_runtime")

        self.stdout.write("PostgreSQL runtime role is ready")
