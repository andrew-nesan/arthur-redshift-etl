"""
Load (or update) data from upstream or execute CTAS or add views to Redshift.

A "load" refers to the wholesale replacement of any schema or table involved.

An "update" refers to the gentle replacement of tables.


There are three possibilities:

(1) "Tables" that have upstream sources must have CSV files and a manifest file.

(2) "CTAS" tables are derived from queries so must have a SQL file.  (Think of them
as materialized views.)

(3) "VIEWS" are views and so must have a SQL file.


Details for (1):

CSV files must have fields delimited by commas, quotes around fields if they
contain a comma, and have doubled-up quotes if there's a quote within the field.

Data format parameters: DELIMITER ',' ESCAPE REMOVEQUOTES GZIP

TODO What is the date format and timestamp format of Sqoop and Spark?

Details for (2):

Expects for every derived table (CTAS) a SQL file in the S3 bucket with a valid
expression to create the content of the table (meaning: just the select without
closing ';'). The actual DDL statement (CREATE TABLE AS ...) and the table
attributes / constraints are added from the matching table design file.

Note that the table is actually created empty, then CTAS is used for a temporary
table which is then inserted into the table.  This is needed to attach
constraints, attributes, and encodings.
"""

import logging
from contextlib import closing
from itertools import chain
from typing import List, Set

import psycopg2
from psycopg2.extensions import connection  # only for type annotation

import etl
import etl.dw
import etl.monitor
import etl.pg
import etl.relation
from etl.config.dw import DataWarehouseSchema
from etl.errors import MissingManifestError, RequiredRelationLoadError, FailedConstraintError
from etl.names import join_column_list, join_with_quotes, TableSelector
from etl.relation import RelationDescription


logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())


def _build_constraints(table_design):
    constraints = table_design.get("constraints", [])
    ddl_constraints = []
    for pk in ("primary_key", "surrogate_key"):
        for constraint in constraints:
            if pk in constraint:
                ddl_constraints.append('PRIMARY KEY ( {} )'.format(join_column_list(constraint[pk])))
    for nk in ("unique", "natural_key"):
        for constraint in constraints:
            if nk in constraint:
                ddl_constraints.append('UNIQUE ( {} )'.format(join_column_list(constraint[nk])))
    return ddl_constraints


def _build_attributes(table_design, exclude_distribution=False):
    attributes = table_design.get("attributes", {})
    ddl_attributes = []
    if "distribution" in attributes and not exclude_distribution:
        dist = attributes["distribution"]
        if isinstance(dist, list):
            ddl_attributes.append('DISTSTYLE KEY')
            ddl_attributes.append('DISTKEY ( {} )'.format(join_column_list(dist)))
        elif dist in ("all", "even"):
            ddl_attributes.append('DISTSTYLE {}'.format(dist.upper()))
    if "compound_sort" in attributes:
        ddl_attributes.append('COMPOUND SORTKEY ( {} )'.format(join_column_list(attributes["compound_sort"])))
    elif "interleaved_sort" in attributes:
        ddl_attributes.append('INTERLEAVED SORTKEY ( {} )'.format(join_column_list(attributes["interleaved_sort"])))
    return ddl_attributes


def assemble_table_ddl(table_design, table_name, use_identity=False, is_temp=False):
    """
    Assemble the DDL to create the table for this design.

    Columns must have a name and a SQL type (compatible with Redshift).
    They may have an attribute of the compression encoding and the nullable
    constraint.
    Other column attributes and constraints should be resolved as table
    attributes (e.g. distkey) and table constraints (e.g. primary key).
    Tables may have attributes such as a distribution style and sort key.
    Depending on the distribution style, they may also have a distribution key.
    Supported table constraints include primary key (most likely "id"),
    unique constraint, and foreign keys.
    """
    s_columns = []
    for column in table_design["columns"]:
        if column.get("skipped", False):
            continue
        f_column = '"{name}" {sql_type}'
        if column.get("identity", False) and use_identity:
            f_column += " IDENTITY(1, 1)"
        if "encoding" in column:
            f_column += " ENCODE {encoding}"
        if column.get("not_null", False):
            f_column += " NOT NULL"
        if column.get("references") and not is_temp:
            # Split column constraint into the table and columns that are referenced
            foreign_table, foreign_columns = column["references"]
            column.update({"foreign_table": foreign_table,
                           "foreign_column": join_column_list(foreign_columns)})
            f_column += " REFERENCES {foreign_table} ( {foreign_column} )"
        s_columns.append(f_column.format(**column))
    s_constraints = _build_constraints(table_design)
    s_attributes = _build_attributes(table_design, exclude_distribution=is_temp)
    table_type = "TEMP TABLE" if is_temp else "TABLE"

    return "CREATE {} IF NOT EXISTS {} (\n{})\n{}".format(table_type, table_name,
                                                          ",\n".join(chain(s_columns, s_constraints)),
                                                          "\n".join(s_attributes)).replace('\n', "\n    ")


def create_table(conn, relation, drop_table=False, dry_run=False):
    """
    Run the CREATE TABLE statement before trying to copy data into table.
    Also assign ownership to make sure all tables are owned by same user.
    Table may be dropped before (re-)creation but only the table owner is
    allowed to do so.
    """
    table_name = relation.target_table_name
    table_design = relation.table_design
    ddl_stmt = assemble_table_ddl(table_design, table_name)

    if dry_run:
        logger.info("Dry-run: Skipping creation of table '%s'", table_name.identifier)
        logger.debug("Skipped DDL:\n%s", ddl_stmt)
    else:
        if drop_table:
            logger.info("Dropping table '%s'", table_name.identifier)
            etl.pg.execute(conn, "DROP TABLE IF EXISTS {} CASCADE".format(table_name))
        logger.info("Creating table '%s' (if not exists)", table_name.identifier)
        etl.pg.execute(conn, ddl_stmt)


def create_view(conn, relation, drop_view=False, dry_run=False):
    """
    Run the CREATE VIEW statement after dropping (potentially) an existing one.
    NOTE that this a no-op if drop_view is False.
    """
    view_name = relation.target_table_name
    s_columns = join_column_list(column["name"] for column in relation.table_design["columns"])
    ddl_stmt = """CREATE VIEW {} (\n{}\n) AS\n{}""".format(view_name, s_columns, relation.query_stmt)
    if drop_view:
        if dry_run:
            logger.info("Dry-run: Skipping (re-)creation of view '%s'", view_name.identifier)
            logger.debug("Skipped DDL:\n%s", ddl_stmt)
        else:
            logger.info("Dropping view (if exists) '%s'", view_name.identifier)
            etl.pg.execute(conn, "DROP VIEW IF EXISTS {} CASCADE".format(view_name))
            logger.info("Creating view '%s'", view_name.identifier)
            etl.pg.execute(conn, ddl_stmt)
    else:
        logger.info("Skipping update of view '%s'", view_name.identifier)
        logger.debug("Skipped DDL:\n%s", ddl_stmt)


def copy_data(conn, relation, aws_iam_role, skip_copy=False, dry_run=False):
    """
    Load data into table in the data warehouse using the COPY command.
    A manifest for the CSV files must be provided -- it is an error if the manifest is missing.

    Tables can only be truncated by their owners (and outside of a transaction), so this will delete
    all rows instead of truncating the tables.
    """
    credentials = "aws_iam_role={}".format(aws_iam_role)
    s3_path = "s3://{}/{}".format(relation.bucket_name, relation.manifest_file_name)
    table_name = relation.target_table_name

    if dry_run:
        if not relation.has_manifest:
            logger.warning("Missing manifest file for '%s'", relation.identifier)
        logger.info("Dry-run: Skipping copy for '%s' from '%s'", table_name.identifier, s3_path)
    elif skip_copy:
        logger.info("Skipping copy for '%s' from '%s'", table_name.identifier, s3_path)
    else:
        if not relation.has_manifest:
            raise MissingManifestError("Missing manifest file for '{}'".format(relation.identifier))

        logger.info("Copying data into '%s' from '%s'", table_name.identifier, s3_path)
        try:
            # FIXME Use NOCOPY during dry-run
            # The connection should not be open with autocommit at this point or we may have empty random tables.
            etl.pg.execute(conn, """DELETE FROM {}""".format(table_name))
            # N.B. If you change the COPY options, make sure to change the documentation at the top of the file.
            etl.pg.execute(conn, """
                COPY {}
                FROM %s
                CREDENTIALS %s MANIFEST
                DELIMITER ',' ESCAPE REMOVEQUOTES GZIP
                TIMEFORMAT AS 'auto' DATEFORMAT AS 'auto'
                TRUNCATECOLUMNS
                """.format(table_name), (s3_path, credentials))
            # TODO Retrieve list of files that were actually loaded
            row_count = etl.pg.query(conn, "SELECT pg_last_copy_count()")
            logger.info("Copied %d rows into '%s'", row_count[0][0], table_name.identifier)
        except psycopg2.Error as exc:
            conn.rollback()
            if "stl_load_errors" in exc.pgerror:
                logger.debug("Trying to get error message from stl_log_errors table")
                info = etl.pg.query(conn, """
                    SELECT query, starttime, filename, colname, type, col_length,
                           line_number, position, err_code, err_reason
                      FROM stl_load_errors
                     WHERE session = pg_backend_pid()
                     ORDER BY starttime DESC
                     LIMIT 1""")
                values = "  \n".join(["{}: {}".format(k, row[k]) for row in info for k in row.keys()])
                logger.info("Information from stl_load_errors:\n  %s", values)
            raise


def assemble_ctas_ddl(table_design, temp_name, query_stmt):
    """
    Return statement to create table based on a query, something like:
    CREATE TEMP TABLE table_name ( column_name [, ... ] ) table_attributes AS query
    """
    s_columns = join_column_list(column["name"]
                                 for column in table_design["columns"]
                                 if not (column.get("identity", False) or column.get("skipped", False)))
    # TODO Measure whether adding attributes helps or hurts performance.
    s_attributes = _build_attributes(table_design, exclude_distribution=True)
    return "CREATE TEMP TABLE {} (\n{})\n{}\nAS\n".format(temp_name, s_columns,
                                                          "\n".join(s_attributes)).replace('\n', "\n     ") + query_stmt


def assemble_insert_into_dml(table_design, table_name, temp_name, add_row_for_key_0=False):
    """
    Create an INSERT statement to copy data from temp table to new table.

    If there is an identity column involved, also add the n/a row with key=0.
    Note that for timestamps, an arbitrary point in the past is used if the column
    isn't nullable.
    """
    s_columns = join_column_list(column["name"]
                                 for column in table_design["columns"]
                                 if not column.get("skipped", False))
    if add_row_for_key_0:
        na_values_row = []
        for column in table_design["columns"]:
            if column.get("skipped", False):
                continue
            elif column.get("identity", False):
                na_values_row.append(0)
            else:
                # Use NULL for all null-able columns:
                if not column.get("not_null", False):
                    # Use NULL for any nullable column and use type cast (for UNION ALL to succeed)
                    na_values_row.append("NULL::{}".format(column["sql_type"]))
                elif "timestamp" in column["sql_type"]:
                    # TODO Is this a good value or should timestamps be null?
                    na_values_row.append("'0000-01-01 00:00:00'")
                elif "string" in column["type"]:
                    na_values_row.append("'N/A'")
                elif "boolean" in column["type"]:
                    na_values_row.append("FALSE")
                else:
                    na_values_row.append("0")
        s_values = ", ".join(str(value) for value in na_values_row)
        return """INSERT INTO {}
                    (SELECT
                         {}
                       FROM {}
                      UNION ALL
                     SELECT
                         {})""".format(table_name, s_columns, temp_name, s_values).replace('\n', "\n    ")
    else:
        return """INSERT INTO {}
                    (SELECT {}
                       FROM {})""".format(table_name, s_columns, temp_name)


def create_temp_table_as_and_copy(conn, relation, skip_copy=False, dry_run=False):
    """
    Run the CREATE TABLE AS statement to load data into temp table, then copy into final table.

    Actual implementation:
    (1) If there is a column marked with identity=True, then create a temporary
    table, insert into it (to build key values).  Finally insert the temp table
    into the destination table while adding a row that has key=0 and n/a values.
    (2) Otherwise, create temp table with CTAS then copy into destination table.

    Note that CTAS doesn't allow to specify column types (or encoding or column
    constraints) so we need to have a temp table separate from destination
    table in order to have full flexibility how we define the destination table.
    """
    table_name = relation.target_table_name
    table_design = relation.table_design
    query_stmt = relation.query_stmt

    temp_identifier = '$'.join(("arthur_temp", table_name.table))
    temp_name = '"{}"'.format(temp_identifier)
    has_any_identity = any([column.get("identity", False) for column in table_design["columns"]])

    if has_any_identity:
        ddl_temp_stmt = assemble_table_ddl(table_design, temp_name, use_identity=True, is_temp=True)
        s_columns = join_column_list(column["name"]
                                     for column in table_design["columns"]
                                     if not (column.get("identity", False) or column.get("skipped", False)))
        dml_temp_stmt = "INSERT INTO {} (\n{}\n) (\n{}\n)".format(temp_name, s_columns, query_stmt)
        dml_stmt = assemble_insert_into_dml(table_design, table_name, temp_name,
                                            add_row_for_key_0=table_name.table.startswith("dim_"))
    else:
        ddl_temp_stmt = assemble_ctas_ddl(table_design, temp_name, query_stmt)
        dml_temp_stmt = None
        dml_stmt = assemble_insert_into_dml(table_design, table_name, temp_name)

    if dry_run:
        logger.info("Dry-run: Skipping loading of table '%s' using '%s'", table_name.identifier, temp_identifier)
        logger.debug("Skipped DDL for '%s': %s", temp_identifier, ddl_temp_stmt)
        logger.debug("Skipped DML for '%s': %s", temp_identifier, dml_temp_stmt)
        logger.debug("Skipped DML for '%s': %s", table_name.identifier, dml_stmt)
    elif skip_copy:
        logger.info("Skipping copy for '%s' from query", table_name.identifier)
        logger.debug("Testing query for '%s' (syntax, dependencies, ...)", table_name.identifier)
        etl.pg.explain(conn, query_stmt)
    else:
        logger.info("Creating temp table '%s'", temp_identifier)
        etl.pg.execute(conn, ddl_temp_stmt)
        if dml_temp_stmt:
            logger.info("Filling temp table '%s'", temp_identifier)
            etl.pg.execute(conn, dml_temp_stmt)
        logger.info("Loading table '%s' from temp table '%s'", table_name.identifier, temp_identifier)
        etl.pg.execute(conn, """DELETE FROM {}""".format(table_name))
        etl.pg.execute(conn, dml_stmt)
        etl.pg.execute(conn, """DROP TABLE {}""".format(temp_name))


def create_schemas_after_backup(dsn_etl: dict, schemas: List[DataWarehouseSchema], dry_run=False) -> None:
    """
    Move schemas out of the way by renaming them. Then create new ones.
    """
    with closing(etl.pg.connection(dsn_etl, autocommit=True)) as conn:
        if dry_run:
            logger.info("Dry-run: Skipping backup of schemas and creation")
        else:
            etl.dw.backup_schemas(conn, schemas)
            etl.dw.create_schemas(conn, schemas)


def grant_access(conn: connection, relation: RelationDescription, schema_config: DataWarehouseSchema, dry_run=False):
    """
    Grant privileges on (new) relation based on configuration.

    We always grant all privileges to the ETL user. We may grant read-only access
    or read-write access based on configuration. Note that the access is always based on groups, not users.
    """
    target = relation.target_table_name
    owner, reader_groups, writer_groups = schema_config.owner, schema_config.reader_groups, schema_config.writer_groups

    if dry_run:
        logger.info("Dry-run: Skipping grant of all privileges on '%s' to '%s'", relation.identifier, owner)
    else:
        logger.info("Granting all privileges on '%s' to '%s'", relation.identifier, owner)
        etl.pg.grant_all_to_user(conn, target.schema, target.table, owner)

    if reader_groups:
        if dry_run:
            logger.info("Dry-run: Skipping granting of select access on '%s' to %s",
                        relation.identifier, join_with_quotes(reader_groups))
        else:
            logger.info("Granting select access on '%s' to %s",
                        relation.identifier, join_with_quotes(reader_groups))
            for reader in reader_groups:
                etl.pg.grant_select(conn, target.schema, target.table, reader)

    if writer_groups:
        if dry_run:
            logger.info("Dry-run: Skipping granting of write access on '%s' to %s",
                        relation.identifier, join_with_quotes(writer_groups))
        else:
            logger.info("Granting write access on '%s' to %s",
                        relation.identifier, join_with_quotes(writer_groups))
            for writer in writer_groups:
                etl.pg.grant_select_and_write(conn, target.schema, target.table, writer)


def analyze(conn: connection, table: RelationDescription, dry_run=False) -> None:
    """
    Update table statistics.
    """
    if dry_run:
        logger.info("Dry-run: Skipping analysis of '%s'", table.identifier)
    else:
        logger.info("Running analyze step on table '%s'", table.identifier)
        etl.pg.execute(conn, "ANALYZE {}".format(table))


def vacuum(dsn_etl: str, relations: List[RelationDescription], dry_run=False) -> None:
    """
    Final step ... tidy up the warehouse before guests come over.
    """
    with closing(etl.pg.connection(dsn_etl, autocommit=True)) as conn:
        for relation in relations:
            if dry_run:
                logger.info("Dry-run: Skipping vacuum of '%s'", relation.identifier)
            else:
                logger.info("Running vacuum step on table '%s'", relation.identifier)
                etl.pg.execute(conn, "VACUUM {}".format(relation))


def load_or_update_redshift_relation(conn, relation, credentials, schema, index,
                                     drop=False, skip_copy=False, dry_run=False):
    """
    Load single table from CSV or using a SQL query or create new view.
    """
    table_name = relation.target_table_name
    if relation.is_ctas_relation or relation.is_view_relation:
        object_key = relation.sql_file_name
    else:
        object_key = relation.manifest_file_name

    # TODO The monitor should contain the number of rows that were loaded.
    modified = False
    with etl.monitor.Monitor(table_name.identifier,
                             "load",
                             options=["skip_copy"] if skip_copy else [],
                             source={'bucket_name': relation.bucket_name,
                                     'object_key': object_key},
                             destination={'name': etl.pg.dbname(conn),
                                          'schema': table_name.schema,
                                          'table': table_name.table},
                             index=index,
                             dry_run=dry_run):
        if relation.is_view_relation:
            create_view(conn, relation, drop_view=drop, dry_run=dry_run)
            grant_access(conn, relation, schema, dry_run=dry_run)
        elif relation.is_ctas_relation:
            create_table(conn, relation, drop_table=drop, dry_run=dry_run)
            create_temp_table_as_and_copy(conn, relation, skip_copy=skip_copy, dry_run=dry_run)
            analyze(conn, relation, dry_run=dry_run)
            # TODO What should we do with table data if a constraint violation is detected? Delete it?
            verify_constraints(conn, relation, dry_run=dry_run)
            grant_access(conn, relation, schema, dry_run=dry_run)
            modified = True
        else:
            create_table(conn, relation, drop_table=drop, dry_run=dry_run)
            # Grant access to data source regardless of loading errors (writers may fix load problem outside of ETL)
            grant_access(conn, relation, schema, dry_run=dry_run)
            copy_data(conn, relation, credentials, skip_copy=skip_copy, dry_run=dry_run)
            analyze(conn, relation, dry_run=dry_run)
            verify_constraints(conn, relation, dry_run=dry_run)
            modified = True
        return modified


def evaluate_execution_order(relations, selector, only_first=False, whole_schemas=False):
    """
    Returns a tuple like ( list of relations to executed, set of schemas they're in )

    Relation descriptions are ordered such that loading them in that order will succeed as
    predicted by the `depends_on` fields.

    If you select to use only the first, then the dependency tree is NOT followed.
    It is an error if this option is attempted to be used with possibly more than one
    table selected.

    If you select to widen the update to entire schemas, then, well, entire schemas
    are updated instead of surgically picking up tables.
    """
    complete_sequence = etl.relation.order_by_dependencies(relations)

    selected = etl.relation.find_matches(complete_sequence, selector)
    dirty = set(relation.identifier for relation in selected)

    if only_first:
        if len(selected) != 1:
            raise ValueError("Bad selector, should result in single table being selected")
        if whole_schemas:
            raise ValueError("Cannot elect to pick both, entire schemas and only first relation")
    else:
        dirty.update(relation.identifier for relation in etl.relation.find_dependents(complete_sequence, selected))

    dirty_schemas = {relation.target_table_name.schema
                     for relation in complete_sequence if relation.identifier in dirty}
    if whole_schemas:
        for relation in complete_sequence:
            if relation.target_table_name.schema in dirty_schemas:
                dirty.add(relation.identifier)

    # FIXME move this into load/upgrade/update to have verb correct?
    if len(dirty) == len(complete_sequence):
        logger.info("Decided on updating ALL tables")
    elif len(dirty) == 1:
        logger.info("Decided on updating a SINGLE table: %s", list(dirty)[0])
    else:
        logger.info("Decided on updating %d of %d table(s)", len(dirty), len(complete_sequence))
    return [relation for relation in complete_sequence if relation.identifier in dirty], dirty_schemas


def load_or_update_redshift(data_warehouse, relations, selector, drop=False, stop_after_first=False,
                            no_rollback=False, skip_copy=False, dry_run=False):
    """
    Load table from CSV file or based on SQL query or install new view.

    Tables are matched based on the selector but note that anything downstream is also refreshed
    as long as the dependency is known.

    This is forceful if drop is True ... and replaces anything that might already exist.

    You can skip the COPY command to bring up the database schemas with all tables quickly although without
    any content.  So a load with drop=True, skip_copy=True followed by a load with drop=False, skip_copy=False
    should be a quick way to load data that is "under development" and may not have all dependencies or
    names / types correct.
    """
    whole_schemas = drop and not stop_after_first
    execution_order, involved_schema_names = evaluate_execution_order(
        relations, selector, only_first=stop_after_first, whole_schemas=whole_schemas)
    logger.info("Starting to load %s relation(s)", len(execution_order))

    required_selector = data_warehouse.required_in_full_load_selector
    schema_config_lookup = {schema.name: schema for schema in data_warehouse.schemas}
    involved_schemas = [schema_config_lookup[s] for s in involved_schema_names]
    if whole_schemas:
        create_schemas_after_backup(data_warehouse.dsn_etl, involved_schemas, dry_run=dry_run)

    vacuum_ready = []
    skip_after_fail = set()  # type: Set[str]

    # TODO Add retry here in case we're doing a full reload.
    conn = etl.pg.connection(data_warehouse.dsn_etl, autocommit=whole_schemas)
    with closing(conn) as conn, conn as conn:
        try:
            for i, relation in enumerate(execution_order):
                index = {"current": i+1, "final": len(execution_order)}
                if relation.identifier in skip_after_fail:
                    logger.warning("Skipping load for relation '%s' due to failed dependencies", relation.identifier)
                    continue
                target_schema = schema_config_lookup[relation.target_table_name.schema]
                try:
                    modified = load_or_update_redshift_relation(
                        conn, relation, data_warehouse.iam_role, target_schema, index,
                        drop=drop, skip_copy=skip_copy, dry_run=dry_run)
                    if modified:
                        vacuum_ready.append(relation.target_table_name)
                except Exception as exc:
                    if whole_schemas:
                        dependent_relations = etl.relation.find_dependents(execution_order, [relation])
                        failed_and_required = [relation.identifier for relation in dependent_relations
                                               if relation.is_required]
                        if relation.is_required or failed_and_required:
                            raise RequiredRelationLoadError(relation.identifier, failed_and_required) from exc
                        logger.warning("This failure for '%s' does not harm any relations required by selector '%s':",
                                       relation.identifier, required_selector, exc_info=True)
                        # Make sure we don't try to load any of the dependents
                        dependents = frozenset(relation.identifier for relation in dependent_relations)
                        if dependents:
                            skip_after_fail.update(dependents)
                            logger.warning("Continuing load omitting these dependent relations: %s",
                                           join_with_quotes(dependents))
                    else:
                        raise

        except Exception:
            if whole_schemas:
                if dry_run:
                    logger.info("Dry-run: Skipping restoration of backup in exception handling")
                elif not no_rollback:
                    # Defensively create a new connection to rollback
                    etl.dw.restore_schemas(etl.pg.connection(data_warehouse.dsn_etl, autocommit=whole_schemas),
                                           involved_schemas)
            raise

    # Reconnect to run vacuum outside transaction block
    if vacuum_ready and not drop:
        vacuum(data_warehouse.dsn_etl, vacuum_ready, dry_run=dry_run)


def verify_constraints(conn, relation, dry_run=False) -> None:
    """
    Raises a FailedConstraintError if :relation's target table doesn't obey its declared unique constraints.

    Note that NULL in SQL is never equal to another value. This means for unique constraints that
    rows where (at least) one column is null are not equal even if they have the same values in the
    not-null columns.  See description of unique index in the PostgreSQL documentation:
    https://www.postgresql.org/docs/8.1/static/indexes-unique.html

    For constraints that check "key" values (like 'primary_key'), this warning does not apply since
    the columns must be not null anyways.

    > "Note that a unique constraint does not, by itself, provide a unique identifier because it
    > does not exclude null values."
    https://www.postgresql.org/docs/8.1/static/ddl-constraints.html
    """
    constraints = relation.table_design.get("constraints")
    if constraints is None:
        logger.info("No constraints to verify for '%s'", relation.identifier)
        return

    # To make this work in DataGrip, define '\{(\w+)\}' under Tools -> Database -> User Parameters.
    # Then execute the SQL using command-enter, enter the values for `cols` and `table`, et voila!
    statement_template = """
        SELECT DISTINCT
               {columns}
          FROM {table}
         WHERE {condition}
      GROUP BY {columns}
        HAVING COUNT(*) > 1
         LIMIT 5
    """

    for constraint in constraints:
        for constraint_type, columns in constraint.items():  # "iterate" over single key
            quoted_columns = join_column_list(columns)
            if constraint_type == "unique":
                condition = " AND ".join('"{}" IS NOT NULL'.format(name) for name in columns)
            else:
                condition = "TRUE"
            statement = statement_template.format(columns=quoted_columns, table=relation, condition=condition)
            if dry_run:
                logger.info("Dry-run: Skipping check of %s constraint in '%s' on [%s]",
                            constraint_type, relation.identifier, join_with_quotes(columns))
                logger.debug("Skipped query:\n%s", statement)
            else:
                logger.info("Checking %s constraint in '%s' on [%s]",
                            constraint_type, relation.identifier, join_with_quotes(columns))
                results = etl.pg.query(conn, statement)
                if results:
                    raise FailedConstraintError(relation, constraint_type, columns, results)


def show_dependents(relations: List[RelationDescription], selector: TableSelector):
    """
    List the execution order of loads or updates.

    Relations are marked based on whether they were directly selected or selected as
    part of the propagation of an update.
    They are also marked whether they'd lead to a fatal error since they're required for full load.
    """
    execution_order, involved_schema_names = evaluate_execution_order(relations, selector)
    if len(execution_order) == 0:
        logger.warning("Found no matching relations for: %s", selector)
        return
    logger.info("Involved schemas: %s", join_with_quotes(involved_schema_names))

    selected = frozenset(relation.identifier for relation in execution_order
                         if selector.match(relation.target_table_name))

    affected = set(selected)
    for relation in execution_order:
        if relation.is_view_relation and any(name in affected for name in relation.dependencies):
            affected.add(relation.identifier)
    immediate = frozenset(affected - selected)

    logger.info("Execution order includes %d selected, %d immediate, and %d downstream relation(s)",
                len(selected), len(immediate), len(execution_order) - len(selected) - len(immediate))

    required = [relation for relation in execution_order if relation.is_required]
    logger.info("Execution order includes %d required relation(s)", len(required))

    max_len = max(len(relation.identifier) for relation in execution_order)
    for i, relation in enumerate(execution_order):
        if relation.is_ctas_relation:
            relation_type = "CTAS"
        elif relation.is_view_relation:
            relation_type = "VIEW"
        else:
            relation_type = "DATA"
        if relation.identifier in selected:
            flag = "selected"
        elif relation.identifier in immediate:
            flag = "immediate"
        else:
            flag = "downstream"
        if relation.is_required:
            flag += ", required"
        print("{index:4d} {identifier:{width}s} ({relation_type}) ({flag})".format(
            index=i + 1, identifier=relation.identifier, width=max_len, relation_type=relation_type, flag=flag))
