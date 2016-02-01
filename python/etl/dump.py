"""
Functions to deal with dumping data from PostgreSQL databases to CSV along with "table designs".

Table designs describe the columns, like their name and type, as well as how the data
should be organized once loaded into Redshift, like the distribution style or sort key.
"""


from collections import defaultdict
from datetime import datetime
from fnmatch import fnmatch
import gzip
import logging
import os
import os.path
import re

import psycopg2
from psycopg2 import errorcodes
import simplejson as json

import etl
import etl.load
import etl.pg
import etl.s3


# List of options for COPY statement that dictates CSV format
CSV_WRITE_FORMAT = "FORMAT csv, HEADER true, NULL '\\N'"

# How to create an "id" column that may be a primary key if none exists in the table
ID_EXPRESSION = "row_number() OVER()"

# Total number of header lines written
N_HEADER_LINES = 3


class MissingMappingError(ValueError):
    """Exception when an attribute type's target type is unknown"""
    pass


def fetch_tables(cx, table_whitelist, table_blacklist, table_pattern=None):
    """
    Retrieve all tables that match the given list of tables (which look like
    schema.name or schema.*) and return them as a list of (schema, table) tuples.

    The first list of patterns defines all tables ever accessible, the
    second list allows to exclude lists from consideration and finally the
    table pattern allows to select specific tables (via command line args).
    """
    # Look for 'r'elations and 'm'aterialized views in the catalog.
    found = etl.pg.query(cx, """SELECT nsp.nspname AS "schema"
                                     , cls.relname AS "table"
                                     , nsp.nspname || '.' || cls.relname AS "table_name"
                                  FROM pg_catalog.pg_class cls
                                  JOIN pg_catalog.pg_namespace nsp ON cls.relnamespace = nsp.oid
                                 WHERE cls.relname NOT LIKE 'tmp%%'
                                   AND cls.relkind IN ('r', 'm')
                                 ORDER BY nsp.nspname, cls.relname""", debug=True)
    tables = []
    for row in found:
        for pattern in table_blacklist:
            if fnmatch(row['table_name'], pattern):
                break
        else:
            for pattern in table_whitelist:
                if fnmatch(row['table_name'], pattern):
                    if table_pattern is None or fnmatch(row['table'], table_pattern):
                        tables.append(etl.TableName(row['schema'], row['table']))

    logging.getLogger(__name__).info("Found %d table(s) matching patterns; whitelist=%s, blacklist=%s, subset=%s",
                                     len(tables), table_whitelist, table_blacklist,
                                     table_pattern if table_pattern else '*')
    return tables


def fetch_columns(cx, tables):
    """
    Retrieve table definitions (column names and types).
    """
    columns = {}
    for table_name in tables:
        ddl = etl.pg.query(cx, """SELECT ca.attname AS attribute,
                                         pg_catalog.format_type(ct.oid, ca.atttypmod) AS attribute_type,
                                         ct.typelem <> 0 AS is_array_type,
                                         pg_catalog.format_type(ce.oid, ca.atttypmod) AS element_type,
                                         ca.attnotnull AS not_null_constraint
                                    FROM pg_catalog.pg_attribute AS ca
                                    JOIN pg_catalog.pg_class AS cls ON ca.attrelid = cls.oid
                                    JOIN pg_catalog.pg_namespace AS ns ON cls.relnamespace = ns.oid
                                    JOIN pg_catalog.pg_type AS ct ON ca.atttypid = ct.oid
                                    LEFT JOIN pg_catalog.pg_type AS ce ON ct.typelem = ce.oid
                                   WHERE ca.attnum > 0  -- skip system columns
                                         AND NOT ca.attisdropped
                                         AND ns.nspname = %s
                                         AND cls.relname = %s
                                   ORDER BY ca.attnum""",
                           (table_name.schema, table_name.table), debug=True)
        columns[table_name] = ddl
        logging.getLogger(__name__).info("Found {} column(s) in {}".format(len(ddl), table_name.identifier))
    return columns


def map_types_in_ddl(columns_by_table, as_is_att_type, cast_needed_att_type):
    """"
    Replace unsupported column types by supported ones and determine casting
    spell.

    Result for every table is a list of tuples with name, old type, new type,
    expression information where the expression within a SELECT will return
    the value of the attribute with the "new" type, serialization type, and
    not null constraint (boolean).

    If the original table definition is missing an "id" column, then one is
    added in the target definition.  The type of the original column is set
    to "<missing>" in this case.
    """
    defs = defaultdict(list)
    for table_name in sorted(columns_by_table):
        found_id = False
        for column in columns_by_table[table_name]:
            attribute_name = column["attribute"]
            attribute_type = column["attribute_type"]
            if attribute_name == "id":
                found_id = True
            for re_att_type, avro_type in as_is_att_type.items():
                if re.match('^' + re_att_type + '$', attribute_type):
                    # Keep the type, use no expression, and pick Avro type from map.
                    mapping = (attribute_type, None, avro_type)
                    break
            else:
                for re_att_type, mapping in cast_needed_att_type.items():
                    if re.match(re_att_type, attribute_type):
                        # Found tuple with new SQL type, expression and Avro type.  Rejoice.
                        break
                else:
                    raise MissingMappingError("Unknown type '{}' of {}.{}.{}".format(attribute_type,
                                                                                     table_name.schema,
                                                                                     table_name.table,
                                                                                     attribute_name))
            delimited_name = '"%s"' % attribute_name
            defs[table_name].append(etl.ColumnDefinition(name=attribute_name,
                                                         source_sql_type=attribute_type,
                                                         sql_type=mapping[0],
                                                         # Replace %s in the column expression by the column name.
                                                         expression=(mapping[1] % delimited_name if mapping[1]
                                                                     else None),
                                                         # Add "null" to Avro type if column may have nulls.
                                                         type=(mapping[2] if column["not_null_constraint"]
                                                               else ["null", mapping[2]]),
                                                         not_null=column["not_null_constraint"]))
        # All tables in the data warehouse are expected to have an id column that can be its primary key.
        if not found_id:
            defs[table_name].insert(0, etl.ColumnDefinition(name="id",
                                                            source_sql_type="<missing>",
                                                            sql_type="bigint",
                                                            expression=ID_EXPRESSION,
                                                            type="long",
                                                            not_null=True))
    return defs


def save_table_design(source_name, table_name, columns, output_dir, overwrite=False, dry_run=False):
    """
    Write new YAML file that defines table columns (starting point for table
    design files) or picks up existing file.  Return filename of new file (or
    None if no file was actually written).
    """
    target_table_name = etl.TableName(source_name, table_name.table)
    filename = os.path.join(output_dir, "{}-{}.yml".format(table_name.schema, table_name.table))
    logger = logging.getLogger(__name__)
    if dry_run:
        logger.info("Dry-run: Skipping writing new table design file for %s", target_table_name.identifier)
    elif not overwrite and os.path.exists(filename):
        # TODO Validate table design against columns found for this table
        logger.warning("Skipping table design for %s since '%s' already exists", table_name.identifier, filename)
    else:
        logger.info("Writing new table design file for %s (was %s) to '%s'",
                    target_table_name.identifier, table_name.identifier, filename)
        table_design = {
            "type": "record",
            "name": "%s" % target_table_name.identifier,
            "source_name": "%s.%s" % (source_name, table_name.identifier),
            "fields": [column._asdict() for column in columns],
            "table_constraints": {
                "primary_key": ["id"]
            },
            "table_attributes": {
                "diststyle": "even",
                "sortkey": ["id"]
            }
        }
        # Remove empty expressions since columns can be selected by name and redundant source_sql_type.
        for column in table_design["fields"]:
            if column["expression"] is None:
                del column["expression"]
            if column["source_sql_type"] == column["sql_type"]:
                del column["source_sql_type"]
        etl.load.validate_table_design(table_design, target_table_name)
        with open(filename, 'w') as o:
            # JSON pretty printing is prettier than o.write(json.dump(table_design, ...))
            json.dump(table_design, o, indent="    ", sort_keys=True)
            o.write('\n')
        return filename
    return None


def create_copy_statement(table_name, columns, row_limit=None):
    """
    Assemble COPY statement that will extract attributes with their new types
    """
    select_column = []
    for column in columns:
        # This is either as-is or an expression with cast or function.
        if column.expression:
            select_column.append(column.expression + ' AS "%s"' % column.name)
        else:
            select_column.append(column.name)
    if row_limit:
        limit = "LIMIT {:d}".format(row_limit)
    else:
        limit = ""
    return "COPY (SELECT {}\n  FROM {}\n{}) TO STDOUT WITH ({})".format(",\n".join(select_column),
                                                                        table_name,
                                                                        limit,
                                                                        CSV_WRITE_FORMAT)


def download_table_data(cx, source_name, table_name, columns, output_dir, limit=None, overwrite=False, dry_run=False):
    """
    Download data (with casts for columns as needed) and compress output files.
    Return filename (if file was successfully created).

    This will skip writing files if they already exist, allowing easy re-starts.

    There are three header lines (timestamp, copy options, column names).  They
    must be skipped when reading the CSV file into Redshift. See HEADER_LINES constant.
    """
    target_table_name = etl.TableName(source_name, table_name.table)
    filename = os.path.join(output_dir, "{}-{}.csv.gz".format(table_name.schema, table_name.table))
    logger = logging.getLogger(__name__)
    if dry_run:
        logger.info("Dry-run: Skipping writing CSV file for %s", target_table_name.identifier)
    elif not overwrite and os.path.exists(filename):
        logger.warning("Skipping CSV data for %s since '%s' already exists", target_table_name.identifier, filename)
    else:
        logger.info("Writing CSV data for %s to '%s'", target_table_name.identifier, filename)
        try:
            with gzip.open(filename, 'wb') as o:
                o.write("# Timestamp: {:%Y-%m-%d %H:%M:%S}\n".format(datetime.now()).encode())
                if limit:
                    o.write("# Copy options with LIMIT {:d}: {}\n".format(limit, CSV_WRITE_FORMAT).encode())
                else:
                    o.write("# Copy options: {}\n".format(CSV_WRITE_FORMAT).encode())
                sql = create_copy_statement(table_name, columns, limit)
                with cx.cursor() as cursor:
                    cursor.copy_expert(sql, o)
        except (Exception, KeyboardInterrupt) as exc:
            logger.warning("Deleting '%s' because it is incomplete", filename)
            os.remove(filename)
            if isinstance(exc, psycopg2.Error):
                etl.pg.log_sql_error(exc)
                if exc.pgcode == errorcodes.INSUFFICIENT_PRIVILEGE:
                    logger.warning("Ignoring denied access for %s", table_name.identifier)
                    return None
                else:
                    raise
            else:
                logger.exception("Trouble while downloading table data: %s", exc)
                raise
        return filename
    return None


def download_table_data_with_sem(sem, cx, source_name, table_name, columns, output_dir,
                                 limit=None, overwrite=False, dry_run=False):
    with sem:
        return download_table_data(cx, source_name, table_name, columns, output_dir,
                                   limit=limit, overwrite=overwrite, dry_run=dry_run)
