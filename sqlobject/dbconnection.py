from __future__ import generators

True, False = 1==1, 0==1

import threading
import re
import warnings
import atexit
import os
import new
import sqlbuilder
from cache import CacheSet
import col
from joins import sorter
from converters import sqlrepr

warnings.filterwarnings("ignore", "DB-API extension cursor.lastrowid used")

_connections = {}

class DBConnection:

    def __init__(self, name=None, debug=False, debugOutput=False,
                 cache=True, style=None, autoCommit=True):
        self.name = name
        self.debug = debug
        self.debugOutput = debugOutput
        self.cache = CacheSet(cache=cache)
        self.doCache = cache
        self.style = style
        self._connectionNumbers = {}
        self._connectionCount = 1
        self.autoCommit = autoCommit
        registerConnectionInstance(self)

    def uri(self):
        auth = self.user or ''
        if auth:
            if self.password:
                auth = auth + '@' + self.password
            auth = auth + ':'
        else:
            assert not password, 'URIs cannot express passwords without usernames'
        uri = '%s://%s' % (self.dbName, auth)
        if self.host:
            uri += self.host + '/'
        if path.startswith('/'):
            path = path[1:]
        return uri + path

    def isSupported(cls):
        raise NotImplemented
    isSupported = classmethod(isSupported)

    def connectionFromURI(cls, uri):
        raise NotImplemented
    connectionFromURI = classmethod(connectionFromURI)

    def _parseURI(uri):
        schema, rest = uri.split(':', 1)
        assert rest.startswith('/'), "URIs must start with scheme:/ -- you did not include a / (in %r)" % rest
        if rest.startswith('/') and not rest.startswith('//'):
            host = None
            rest = rest[1:]
        elif rest.startswith('///'):
            host = None
            rest = rest[3:]
        else:
            rest = rest[2:]
            if rest.find('/') == -1:
                host = rest
                rest = ''
            else:
                host, rest = rest.split('/', 1)
        if host and host.find('@') != -1:
            user, host = host.split('@', 1)
            if user.find(':') != -1:
                user, password = user.split(':', 1)
            else:
                password = None
        else:
            user = password = None
        path = '/' + rest
        return user, password, host, path
    _parseURI = staticmethod(_parseURI)

class DBAPI(DBConnection):

    """
    Subclass must define a `makeConnection()` method, which
    returns a newly-created connection object.

    ``queryInsertID`` must also be defined.
    """

    dbName = None

    def __init__(self, **kw):
        self._pool = []
        self._poolLock = threading.Lock()
        DBConnection.__init__(self, **kw)

    def _runWithConnection(self, meth, *args):
        conn = self.getConnection()
        try:
            val = meth(conn, *args)
        finally:
            self.releaseConnection(conn)
        return val

    def getConnection(self):
        self._poolLock.acquire()
        try:
            if not self._pool:
                newConn = self.makeConnection()
                self._pool.append(newConn)
                self._connectionNumbers[id(newConn)] = self._connectionCount
                self._connectionCount += 1
            val = self._pool.pop()
            return val
        finally:
            self._poolLock.release()

    def releaseConnection(self, conn):
        if self.supportTransactions:
            if self.autoCommit == 'exception':
                if self.debug:
                    self.printDebug(conn, 'auto/exception', 'ROLLBACK')
                conn.rollback()
                raise Exception, 'Object used outside of a transaction; implicit COMMIT or ROLLBACK not allowed'
            elif self.autoCommit:
                if self.debug:
                    self.printDebug(conn, 'auto', 'COMMIT')
                conn.commit()
            else:
                if self.debug:
                    self.printDebug(conn, 'auto', 'ROLLBACK')
                conn.rollback()
        if self._pool is not None:
            self._pool.append(conn)

    def printDebug(self, conn, s, name, type='query'):
        if type == 'query':
            sep = ': '
        else:
            sep = '->'
            s = repr(s)
        n = self._connectionNumbers[id(conn)]
        spaces = ' '*(8-len(name))
        print '%(n)2i/%(name)s%(spaces)s%(sep)s %(s)s' % locals()

    def _query(self, conn, s):
        if self.debug:
            self.printDebug(conn, s, 'Query')
        conn.cursor().execute(s)

    def query(self, s):
        return self._runWithConnection(self._query, s)

    def _queryAll(self, conn, s):
        if self.debug:
            self.printDebug(conn, s, 'QueryAll')
        c = conn.cursor()
        c.execute(s)
        value = c.fetchall()
        if self.debugOutput:
            self.printDebug(conn, value, 'QueryAll', 'result')
        return value

    def queryAll(self, s):
        return self._runWithConnection(self._queryAll, s)

    def _queryOne(self, conn, s):
        if self.debug:
            self.printDebug(conn, s, 'QueryOne')
        c = conn.cursor()
        c.execute(s)
        value = c.fetchone()
        if self.debugOutput:
            self.printDebug(conn, value, 'QueryOne', 'result')
        return value

    def queryOne(self, s):
        return self._runWithConnection(self._queryOne, s)

    def _insertSQL(self, table, names, values):
        return ("INSERT INTO %s (%s) VALUES (%s)" %
                (table, ', '.join(names),
                 ', '.join([self.sqlrepr(v) for v in values])))

    def transaction(self):
        return Transaction(self)

    def queryInsertID(self, table, idName, id, names, values):
        return self._runWithConnection(self._queryInsertID, table, idName, id, names, values)

    def _iterSelect(self, conn, select, withConnection=None,
                    keepConnection=False):
        cursor = conn.cursor()
        query = self.queryForSelect(select)
        if self.debug:
            self.printDebug(conn, query, 'Select')
        cursor.execute(query)
        while 1:
            result = cursor.fetchone()
            if result is None:
                if not keepConnection:
                    self.releaseConnection(conn)
                break
            if select.ops.get('lazyColumns', 0):
                obj = select.sourceClass.get(result[0], connection=withConnection)
                yield obj
            else:
                obj = select.sourceClass.get(result[0], selectResults=result[1:], connection=withConnection)
                yield obj

    def iterSelect(self, select):
        return self._runWithConnection(self._iterSelect, select, self,
                                       False)

    def countSelect(self, select):
        q = "SELECT COUNT(*) FROM %s WHERE" % \
            ", ".join(select.tables)
        q = self._addWhereClause(select, q, limit=0, order=0)
        val = int(self.queryOne(q)[0])
        return val

    def queryForSelect(self, select):
        ops = select.ops
        cls = select.sourceClass
        if ops.get('lazyColumns', 0):
            q = "SELECT %s.%s FROM %s WHERE " % \
                (cls._table, cls._idName,
                 ", ".join(select.tables))
        else:
            q = "SELECT %s.%s, %s FROM %s WHERE " % \
                (cls._table, cls._idName,
                 ", ".join(["%s.%s" % (cls._table, col.dbName)
                            for col in cls._SO_columns]),
                 ", ".join(select.tables))

        return self._addWhereClause(select, q)

    def _addWhereClause(self, select, startSelect, limit=1, order=1):

        q = select.clause
        if type(q) not in [type(''), type(u'')]:
            q = self.sqlrepr(q)
        ops = select.ops

        def clauseList(lst, desc=False):
            if type(lst) not in (type([]), type(())):
                lst = [lst]
            lst = [clauseQuote(i) for i in lst]
            if desc:
                lst = [sqlbuilder.DESC(i) for i in lst]
            return ', '.join([self.sqlrepr(i) for i in lst])

        def clauseQuote(s):
            if type(s) is type(""):
                if s.startswith('-'):
                    desc = True
                    s = s[1:]
                else:
                    desc = False
                assert sqlbuilder.sqlIdentifier(s), "Strings in clauses are expected to be column identifiers.  I got: %r" % s
                if select.sourceClass._SO_columnDict.has_key(s):
                    s = select.sourceClass._SO_columnDict[s].dbName
                if desc:
                    return sqlbuilder.DESC(sqlbuilder.SQLConstant(s))
                else:
                    return sqlbuilder.SQLConstant(s)
            else:
                return s

        if order and ops.get('dbOrderBy'):
            q = "%s ORDER BY %s" % (q, clauseList(ops['dbOrderBy'], ops.get('reversed', False)))

        start = ops.get('start', 0)
        end = ops.get('end', None)

        q = startSelect + ' ' + q

        if limit and (start or end):
            # @@: Raising an error might be an annoyance, but some warning is
            # in order.
            #assert ops.get('orderBy'), "Getting a slice of an unordered set is unpredictable!"
            q = self._queryAddLimitOffset(q, start, end)

        return q

    def _SO_createJoinTable(self, join):
        self.query('CREATE TABLE %s (\n%s %s,\n%s %s\n)' %
                   (join.intermediateTable,
                    join.joinColumn,
                    self.joinSQLType(join),
                    join.otherColumn,
                    self.joinSQLType(join)))

    def _SO_dropJoinTable(self, join):
        self.query("DROP TABLE %s" % join.intermediateTable)

    def createTable(self, soClass):
        self.query('CREATE TABLE %s (\n%s\n)' % \
                   (soClass._table, self.createColumns(soClass)))

    def createColumns(self, soClass):
        columnDefs = [self.createIDColumn(soClass)] \
                     + [self.createColumn(soClass, col)
                        for col in soClass._SO_columns]
        return ",\n".join(["    %s" % c for c in columnDefs])

    def createColumn(self, soClass, col):
        assert 0, "Implement in subclasses"

    def dropTable(self, tableName, cascade=False):
        self.query("DROP TABLE %s" % tableName)

    def clearTable(self, tableName):
        # 3-03 @@: Should this have a WHERE 1 = 1 or similar
        # clause?  In some configurations without the WHERE clause
        # the query won't go through, but maybe we shouldn't override
        # that.
        self.query("DELETE FROM %s" % tableName)

    # The _SO_* series of methods are sorts of "friend" methods
    # with SQLObject.  They grab values from the SQLObject instances
    # or classes freely, but keep the SQLObject class from accessing
    # the database directly.  This way no SQL is actually created
    # in the SQLObject class.

    def _SO_update(self, so, values):
        self.query("UPDATE %s SET %s WHERE %s = %s" %
                   (so._table,
                    ", ".join(["%s = %s" % (dbName, self.sqlrepr(value))
                               for dbName, value in values]),
                    so._idName,
                    self.sqlrepr(so.id)))

    def _SO_selectOne(self, so, columnNames):
        return self.queryOne("SELECT %s FROM %s WHERE %s = %s" %
                             (", ".join(columnNames),
                              so._table,
                              so._idName,
                              self.sqlrepr(so.id)))

    def _SO_selectOneAlt(self, cls, columnNames, column, value):
        return self.queryOne("SELECT %s FROM %s WHERE %s = %s" %
                             (", ".join(columnNames),
                              cls._table,
                              column,
                              self.sqlrepr(value)))

    def _SO_delete(self, so):
        self.query("DELETE FROM %s WHERE %s = %s" %
                   (so._table,
                    so._idName,
                    self.sqlrepr(so.id)))

    def _SO_selectJoin(self, soClass, column, value):
        return self.queryAll("SELECT %s FROM %s WHERE %s = %s" %
                             (soClass._idName,
                              soClass._table,
                              column,
                              self.sqlrepr(value)))

    def _SO_intermediateJoin(self, table, getColumn, joinColumn, value):
        return self.queryAll("SELECT %s FROM %s WHERE %s = %s" %
                             (getColumn,
                              table,
                              joinColumn,
                              self.sqlrepr(value)))

    def _SO_intermediateDelete(self, table, firstColumn, firstValue,
                               secondColumn, secondValue):
        self.query("DELETE FROM %s WHERE %s = %s AND %s = %s" %
                   (table,
                    firstColumn,
                    self.sqlrepr(firstValue),
                    secondColumn,
                    self.sqlrepr(secondValue)))

    def _SO_intermediateInsert(self, table, firstColumn, firstValue,
                               secondColumn, secondValue):
        self.query("INSERT INTO %s (%s, %s) VALUES (%s, %s)" %
                   (table,
                    firstColumn,
                    secondColumn,
                    self.sqlrepr(firstValue),
                    self.sqlrepr(secondValue)))

    def _SO_columnClause(self, soClass, kw):
        return ' '.join(['%s = %s' %
                         (soClass._SO_columnDict[key].dbName,
                          self.sqlrepr(value))
                         for key, value
                         in kw.items()])

    def sqlrepr(self, v):
        return sqlrepr(v, self.dbName)


class Transaction(object):

    def __init__(self, dbConnection):
        self._dbConnection = dbConnection
        self._connection = dbConnection.getConnection()
        self._dbConnection._setAutoCommit(self._connection, 0)
        self.cache = CacheSet(cache=dbConnection.doCache)

    def query(self, s):
        return self._dbConnection._query(self._connection, s)

    def queryAll(self, s):
        return self._dbConnection._queryAll(self._connection, s)

    def queryOne(self, s):
        return self._dbConnection._queryOne(self._connection, s)

    def queryInsertID(self, table, idName, id, names, values):
        return self._dbConnection._queryInsertID(
            self._connection, table, idName, id, names, values)

    def iterSelect(self, select):
        # @@: Bad stuff here, because the connection will be used
        # until the iteration is over, or at least a cursor from
        # the connection, which not all database drivers support.
        return self._dbConnection._iterSelect(
            self._connection, select, withConnection=self,
            keepConnection=True)

    def commit(self):
        if self._dbConnection.debug:
            self._dbConnection.printDebug(self._connection, '', 'COMMIT')
        self._connection.commit()

    def rollback(self):
        if self._dbConnection.debug:
            self._dbConnection.printDebug(self._connection, '', 'ROLLBACK')
        subCaches = [(sub, sub.allIDs()) for sub in self.cache.allSubCaches()]
        self._connection.rollback()

        for subCache, ids in subCaches:
            for id in ids:
                inst = subCache.tryGet(id)
                if inst is not None:
                    inst.expire()

    def __getattr__(self, attr):
        """
        If nothing else works, let the parent connection handle it.
        Except with this transaction as 'self'.  Poor man's
        acquisition?  Bad programming?  Okay, maybe.
        """
        attr = getattr(self._dbConnection, attr)
        try:
            func = attr.im_func
        except AttributeError:
            return attr
        else:
            meth = new.instancemethod(func, self, self.__class__)
            return meth

    def __del__(self):
        self.rollback()
        self._dbConnection.releaseConnection(self._connection)

class ConnectionURIOpener(object):

    def __init__(self):
        self.schemeBuilders = {}
        self.schemeSupported = {}
        self.instanceNames = {}
        self.cachedURIs = {}

    def registerConnection(self, schemes, builder, isSupported):
        for uriScheme in schemes:
            assert not self.schemeBuilders.has_key(uriScheme) \
                   or self.schemeBuilders[uriScheme] is builder, \
                   "A driver has already been registered for the URI scheme %s" % uriScheme
            self.schemeBuilders[uriScheme] = builder
            self.schemeSupported = isSupported

    def registerConnectionInstance(self, inst):
        if inst.name:
            assert not self.instanceNames.has_key(inst.name) \
                   or self.instanceNames[inst.name] is cls, \
                   "A instance has already been registered with the name %s" % inst.name
            assert inst.name.find(':') == -1, "You cannot include ':' in your class names (%r)" % cls.name
            self.instanceNames[inst.name] = inst

    def connectionForURI(self, uri):
        if self.cachedURIs.has_key(uri):
            return self.cachedURIs[uri]
        if uri.find(':') != -1:
            scheme, rest = uri.split(':', 1)
            assert self.schemeBuilders.has_key(scheme), \
                   "No SQLObject driver exists for %s" % scheme
            conn = self.schemeBuilders[scheme]().connectionFromURI(uri)
        else:
            # We just have a name, not a URI
            assert self.instanceNames.has_key(uri), \
                   "No SQLObject driver exists under the name %s" % uri
            conn = self.instanceNames[uri]
        # @@: Do we care if we clobber another connection?
        self.cachedURIs[uri] = conn
        return conn

TheURIOpener = ConnectionURIOpener()

registerConnection = TheURIOpener.registerConnection
registerConnectionInstance = TheURIOpener.registerConnectionInstance
connectionForURI = TheURIOpener.connectionForURI
