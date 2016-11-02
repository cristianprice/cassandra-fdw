from cassandra.util import OrderedMapSerializedKey
from cassandra import ConsistencyLevel
from cassandra.metadata import Metadata
from cassandra.cluster import Cluster
from cassandra.query import SimpleStatement
from collections import defaultdict
from cassandra.auth import PlainTextAuthProvider
from datetime import datetime, date, time, timedelta
from cStringIO import StringIO
import time
import math
import json
import types_mapper
import logger
import operator
from logger import ERROR, WARNING, INFO, DEBUG
from properties import ISDEBUG
from cassandra.concurrent import execute_concurrent


class CassandraProvider:

    REGULAR_QUERY_COST = 10000
    IDX_QUERY_COST = 1000
    CLUSTERING_KEY_QUERY_COST = 100
    PARTITION_KEY_QUERY_COST = 1
    ROWIDCOLUMN = u"__rowid__"
    
    def __init__(self, options, columns):
        start_time = time.time()
        if (("keyspace" not in options) or ("columnfamily" not in options)) and ("query" not in options):
            logger.log("Either query or columnfamily and keyspace parameter is required.", ERROR)
        self.columnfamily = options.get("columnfamily", None)
        self.keyspace = options.get("keyspace", None)
        self.query = options.get("query", None)
        self.init_connection(options, columns)
        start_time1 = time.time()
        self.describe_db()
        end_time = time.time()
        self.insert_stmt = None
        self.delete_stmt = None
        self.prepared_select_stmts = {}
        if ISDEBUG:
            logger.log("DB described in {0} ms".format(int((end_time - start_time1) * 1000)))
            logger.log("initialized in {0} ms".format(int((end_time - start_time) * 1000)))


    def init_connection(self, options, columns):
        start_time = time.time()
        if "hosts" not in options:
            logger.log("The hosts parameter is needed, setting to localhost.", WARNING)
        hosts = options.get("hosts", "localhost").split(",")
        if "port" not in options:
            logger.log("The port parameter is needed, setting to 9042.", WARNING)
        self.port = options.get("port", "9042")
        self.limit = options.get("limit", None)
        self.allow_filtering = options.get("allow_filtering", 'False') == 'True'
        self.enable_trace = options.get("trace", 'False') == 'True'
        timeout = options.get("timeout", None)
        username = options.get("username", None)
        password = options.get("password", None)

        self.cluster = Cluster(hosts)
        if(username is not None):
            self.cluster.auth_provider = PlainTextAuthProvider(username=username, password=password)
        # Cassandra connection init
        self.cluster.executor_threads = 4
        self.session = self.cluster.connect()
        end_time = time.time()
        if ISDEBUG:
            logger.log("connected in {0} ms".format(int((end_time - start_time) * 1000)))
        if (timeout):
            self.session.default_timeout = timeout

    def prepare_insert_stmt(self):
        insert_stmt_str = u"INSERT INTO {0}.{1} ({2}) VALUES ({3})".format(
            self.keyspace, self.columnfamily, u",".join(self.queryableColumns), u",".join([u"?"] * len(self.queryableColumns)))
        if ISDEBUG:
            logger.log("preparing insert statement")
            st = time.time()
        self.insert_stmt = self.session.prepare(insert_stmt_str)
        if ISDEBUG:
            logger.log("insert statement prepared in {0} ms".format((time.time() - st) * 1000))

    def prepare_delete_stmt(self):
        delete_stmt_str = u"DELETE FROM {0}.{1} WHERE {2};".format(self.keyspace, self.columnfamily, u" AND ".join(map(lambda str: str + u" = ?", self.rowIdColumns)))
        if ISDEBUG:
            logger.log("preparing delete statement")
            st = time.time()
        self.delete_stmt = self.session.prepare(delete_stmt_str)
        if ISDEBUG:
            logger.log("delete statement prepared in {0} ms".format((time.time() - st) * 1000))

    def describe_db(self):

        self.queryableColumns = {}
        self.querableColumnsIdx = {}
        self.querableColumnsValidator = {}
        self.rowIdColumns = []

        table = self.cluster.metadata.keyspaces[self.keyspace].tables[self.columnfamily]
        pkeys = [pk.name for pk in table.partition_key]
        ckeys = [ck.name for ck in table.clustering_key]
        idxs = [i.options["target"] for i in table.indexes if "target" in i.options]
        columns = table.columns
        componentIdx = 0
        met_pk = False
        met_ck = False
        met_regular = False
        for c in columns:
            columnName = c
            col = columns[c]
            is_primary_key = False
            if columnName in pkeys:
                cost = self.PARTITION_KEY_QUERY_COST
                is_primary_key = True
                if not met_pk:
                    met_pk = True
                    componentIdx = 0
                else:
                    componentIdx += 1
            elif columnName in ckeys:
                cost = self.CLUSTERING_KEY_QUERY_COST
                is_primary_key = True
                if not met_ck:
                    met_ck = True
                    componentIdx = 0
                else:
                    componentIdx += 1
            else:
                cost = self.REGULAR_QUERY_COST
                if not met_regular:
                    met_regular = True
                    componentIdx = 0
                else:
                    componentIdx += 1
            if columnName in idxs:
                cost = IDX_QUERY_COST
            if is_primary_key:
                self.rowIdColumns.append(columnName)
            self.queryableColumns[columnName] = cost
            self.querableColumnsIdx[columnName] = componentIdx + cost
            self.querableColumnsValidator[columnName] = col.cql_type
        self.preparedColumnsIds = []
        for col in self.queryableColumns:
            self.preparedColumnsIds.append(col)

    def insert(self, new_values):
        if self.insert_stmt is None:
            self.prepare_insert_stmt()
        args = self.get_insert_args(new_values)
        if ISDEBUG:
            logger.log("requested insert {0}".format(args))
            st = time.time()
        self.session.execute(self.insert_stmt, args)
        if ISDEBUG:
            et = time.time()
            logger.log("insert completed in {0} ms".format((et - st) * 1000))
        return new_values

    def get_insert_args(self, new_values):
        sorted_args = []
        for col in self.preparedColumnsIds:
            sorted_args.append(types_mapper.map_object_to_type(new_values[col], self.querableColumnsValidator[col]))
        return sorted_args

    def get_delete_args(self, row_id_value):
        ids = json.loads(row_id_value)
        values = []
        for i in range(0, len(self.rowIdColumns)):
            columnName = self.rowIdColumns[i]
            values.append(types_mapper.map_object_to_type(ids[i], self.querableColumnsValidator[columnName]))
        return values

    def get_insert_stmt(self):
        if self.insert_stmt is None:
            self.prepare_insert_stmt()
        return self.insert_stmt

    def get_delete_stmt(self):
        if self.delete_stmt is None:
            self.prepare_delete_stmt()
        return self.delete_stmt

    def execute_modify_items(self, modify_items, concurency):
        if len(modify_items) == 0:
            return {}
        if ISDEBUG:
            logger.log("start modify operation. count: {0}".format(len(modify_items)))
            st = time.time()
        statements_and_params = []
        for item in modify_items:
            if item[0] == 'insert':
                statements_and_params.append((self.get_insert_stmt(), self.get_insert_args(item[1])))
            elif item[0] == 'delete':
                statements_and_params.append((self.get_delete_stmt(), self.get_delete_args(item[1])))
            else:
                raise ValueError('unknown modify item type')
        if len(statements_and_params) == 1:
            self.session.execute(statements_and_params[0][0], statements_and_params[0][1])
        else:
            execute_concurrent(self.session, statements_and_params, raise_on_first_error=True, concurrency=concurency)
        if ISDEBUG:
            logger.log("modify completed in {0} ms".format((time.time() - st) * 1000))
        

    def delete(self, rowid):
        if self.delete_stmt is None:
            self.prepare_delete_stmt()
        if ISDEBUG:
            logger.log(u"requested delete for id: {0}".format(rowid))
        values = self.get_delete_args(rowid)
        if ISDEBUG:
            st = time.time()
        self.session.execute(self.delete_stmt, values)
        if ISDEBUG:
            et = time.time()
            logger.log("delete completed in {0} ms".format((et - st) * 1000))
        return {}

    def build_select_stmt(self, quals, columns, allow_filtering, verbose=False):
        
        stmt_str = StringIO()
        usedQuals = {}
        filteredColumns = []
        rowid = None
        binding_values = []
        for col in columns:
            if col != self.ROWIDCOLUMN:
                filteredColumns.append(col)
        if (self.query):
            stmt_str.write(self.query)
        else:
            for col in self.rowIdColumns:
                if col not in filteredColumns:
                    filteredColumns.append(col)
            stmt_str.write(u"SELECT {0} FROM {1}.{2}".format(",".join(map(lambda c: '"{0}"'.format(c), filteredColumns)), self.keyspace, self.columnfamily))
            isWhere = None
            eqRestricted = None

            for qual in quals:
                if qual.field_name == self.ROWIDCOLUMN:
                    rowid = qual.value
                if qual.field_name in self.queryableColumns:
                    qual.componentIdx = self.querableColumnsIdx[qual.field_name]
                else:
                    qual.componentIdx = 10000

            if rowid is not None:
                ids = json.loads(rowid)
                for i in range(0, len(self.rowIdColumns)):
                    columnName = self.rowIdColumns[i]
                    binding_values.append(types_mapper.map_object_to_type(ids[i], self.querableColumnsValidator[columnName]))
                stmt_str.write(u" WHERE {0}".format(u" AND ".join(map(lambda str: str + u" = ?", self.rowIdColumns))))
            else:
                sortedQuals = sorted(quals, key=lambda qual: qual.componentIdx)
                last_clustering_key_idx = 0
                for qual in sortedQuals:
                    # Partition key and clustering column can't be null
                    if qual.componentIdx < self.IDX_QUERY_COST and qual.value is None:
                        return None
                    if ISDEBUG or verbose:
                        logger.log(u"qual field {0}; qual index {1}; qual type {2}; qual operator: {4}; qual value {3}".format(qual.field_name, qual.componentIdx, type(qual.operator), qual.value, qual.operator))
                    if qual.operator == "=":
                        if (qual.field_name in self.queryableColumns and self.queryableColumns[qual.field_name] != self.REGULAR_QUERY_COST):
                            if self.queryableColumns[qual.field_name] == self.CLUSTERING_KEY_QUERY_COST:
                                if last_clustering_key_idx == 0 and qual.componentIdx != self.CLUSTERING_KEY_QUERY_COST:
                                    eqRestricted = True
                                elif qual.componentIdx - 1 != last_clustering_key_idx and last_clustering_key_idx != 0:
                                    eqRestricted = True
                            if (qual.field_name not in usedQuals and not eqRestricted):
                                usedQuals[qual.field_name] = qual.value
                                if self.queryableColumns[qual.field_name] == self.CLUSTERING_KEY_QUERY_COST:
                                    last_clustering_key_idx = qual.componentIdx
                                formatted = u" {0} = ? ".format(qual.field_name)
                                binding_values.append(types_mapper.map_object_to_type(qual.value, self.querableColumnsValidator[qual.field_name]))
                                if isWhere:
                                    stmt_str.write(u" AND ")
                                    stmt_str.write(formatted)
                                else:
                                    stmt_str.write(u" WHERE ")
                                    stmt_str.write(formatted)
                                    isWhere = 1
                    # IN operator
                    elif qual.operator == (u"=", True):
                        if (qual.field_name in self.queryableColumns):
                            if (self.queryableColumns[qual.field_name] == self.CLUSTERING_KEY_QUERY_COST or self.queryableColumns[qual.field_name] == self.PARTITION_KEY_QUERY_COST):
                                if (qual.field_name not in usedQuals and not eqRestricted):
                                    usedQuals[qual.field_name] = qual.value
                                    formatted = u"{0} IN ?".format(qual.field_name)
                                    binding_value = []
                                    for el in qual.value:
                                        binding_value.append(types_mapper.map_object_to_type(el, self.querableColumnsValidator[qual.field_name]))
                                    binding_values.append(binding_value)
                                    if isWhere:
                                        stmt_str.write(u" AND ")
                                        stmt_str.write(formatted)
                                    else:
                                        stmt_str.write(u" WHERE ")
                                        stmt_str.write(formatted)
                    else:
                        if (qual.operator == ">" or qual.operator == "<" or qual.operator == ">=" or qual.operator == "<="):
                            if (qual.field_name in self.queryableColumns and self.queryableColumns[qual.field_name] == self.CLUSTERING_KEY_QUERY_COST):
                                if (not eqRestricted or eqRestricted == qual.field_name):
                                    eqRestricted = qual.field_name
                                    if isWhere:
                                        stmt_str.write(u" AND {0} {1} ?".format(qual.field_name, qual.operator))
                                        binding_values.append(types_mapper.map_object_to_type(qual.value, self.querableColumnsValidator[qual.field_name]))
                                    else:
                                        stmt_str.write(u" WHERE {0} {1} ?".format(qual.field_name, qual.operator))
                                        isWhere = 1
                                        binding_values.append(types_mapper.map_object_to_type(qual.value, self.querableColumnsValidator[qual.field_name]))

        if (self.limit):
            stmt_str.write(u" LIMIT ".format(limit))
        if allow_filtering:
            stmt_str.write(u" ALLOW FILTERING ")
        statement = stmt_str.getvalue()
        stmt_str.close()
        if ISDEBUG:
            logger.log(u"CQL query: {0}".format(statement), INFO)
        return (statement, binding_values, filteredColumns)


    def execute(self, quals, columns, sortkeys=None):
        res = self.build_select_stmt(quals, columns, self.allow_filtering)
        if res is None:
            yield {}
            return
        stmt = res[0]
        binding_values = res[1]
        filtered_columns = res[2]
        if stmt not in self.prepared_select_stmts:
            if ISDEBUG:
                logger.log(u"preparing statement...")
            self.prepared_select_stmts[stmt] = self.session.prepare(stmt)
        elif ISDEBUG:
                logger.log(u"statement already prepared")
        if ISDEBUG:
            logger.log(u"executing statement...")
            st = time.time()
        elif self.enable_trace:
            logger.log(u"executing statement '{0}'".format(stmt))
        result = self.session.execute(self.prepared_select_stmts[stmt], binding_values)
        if ISDEBUG:
            logger.log(u"cursor got in {0} ms".format((time.time() - st) * 1000))
        for row in result:
            line = {}
            idx = 0
            for column_name in filtered_columns:
                value = row[idx]
                if self.querableColumnsValidator[column_name] == 'timestamp' and value is not None:
                    line[column_name] = u"{0}+00:00".format(value)
                elif self.querableColumnsValidator[column_name] == 'time' and value is not None:
                    line[column_name] = u"{0}+00:00".format(value)
                elif isinstance(value, tuple):
                    tuple_values = []
                    for t in value:
                        tuple_values.append(str(t))
                    line[column_name] = json.dumps(tuple_values)
                elif isinstance(value, OrderedMapSerializedKey):
                    dict_values = {}
                    for i in value:
                        dict_values[str(i)] = str(value[i])
                    line[column_name] = json.dumps(dict_values)
                else:
                    line[column_name] = value
                idx = idx + 1
            rowid_values = []
            for idcolumn in self.rowIdColumns:
                rowid_values.append(str(line[idcolumn]))
            line[self.ROWIDCOLUMN] = json.dumps(rowid_values)
            yield line

    def get_row_id_column(self):
        if ISDEBUG:
            logger.log(u"rowid requested")
        return self.ROWIDCOLUMN

    def get_path_keys(self):
        output = []
        sorted_items = sorted(self.querableColumnsIdx.items(), key=operator.itemgetter(1))
        clusteting_key_columns = []
        partition_key_columns = []
        idx_columns = []
        regular_columns = []
        ptt = []
        for tp in sorted_items:
            k = tp[0]
            v = tp[1]
            if v >= self.PARTITION_KEY_QUERY_COST and v < self.CLUSTERING_KEY_QUERY_COST:
                partition_key_columns.append(k)
                ptt.append(k)
            if v < self.IDX_QUERY_COST and v >= self.CLUSTERING_KEY_QUERY_COST:
                clusteting_key_columns.append(k)
            elif v >= self.IDX_QUERY_COST and v < self.REGULAR_QUERY_COST:
                idx_columns.append(k)
            else:
                regular_columns.append(k)

        ckc_len = len(clusteting_key_columns)
        if ckc_len == 0:
            output.append((tuple(partition_key_columns), 1))
        else:
            i = 1
            output.append((tuple(partition_key_columns), self.CLUSTERING_KEY_QUERY_COST))
            for ckc in clusteting_key_columns:
                ptt.append(ckc)
                if i == ckc_len:
                    output.append((tuple(ptt), 1))
                else:
                    output.append((tuple(ptt), self.CLUSTERING_KEY_QUERY_COST - i))
                    if len(idx_columns) != 0:
                        output.append((tuple(ptt + idx_columns), self.IDX_QUERY_COST - i))
                i += 1

        for idx_col in idx_columns:
            output.append((tuple(idx_col), self.IDX_QUERY_COST))
        
        for t in sorted_items:
            output.append(((t[0]), self.REGULAR_QUERY_COST))

        output.append(((self.get_row_id_column()), 1))

        if ISDEBUG:
            logger.log('path keys: {0}'.format(output))
        return output