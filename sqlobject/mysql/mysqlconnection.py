from sqlobject.dbconnection import DBAPI
MySQLdb = None

class MySQLConnection(DBAPI):

    supportTransactions = False
    dbName = 'mysql'
    schemes = [dbName]

    def __init__(self, db, user, passwd='', host='localhost', **kw):
        global MySQLdb
        if MySQLdb is None:
            import MySQLdb
        self.host = host
        self.db = db
        self.user = user
        self.passwd = passwd
        DBAPI.__init__(self, **kw)

    def connectionFromURI(cls, uri):
        user, password, host, path = cls._parseURI(uri)
        return cls(db=path.strip('/'), user=user or '', passwd=password or '',
                   host=host or 'localhost')
    connectionFromURI = classmethod(connectionFromURI)

    def isSupported(cls):
        global MySQLdb
        if MySQLdb is None:
            try:
                import MySQLdb
            except ImportError:
                return False
        return True
    isSupported = classmethod(isSupported)

    def makeConnection(self):
        return MySQLdb.connect(host=self.host, db=self.db,
                               user=self.user, passwd=self.passwd)

    def _queryInsertID(self, conn, table, idName, id, names, values):
        c = conn.cursor()
        if id is not None:
            names = [idName] + names
            values = [id] + values
        q = self._insertSQL(table, names, values)
        if self.debug:
            self.printDebug(conn, q, 'QueryIns')
        c.execute(q)
        if id is None:
            id = c.insert_id()
        if self.debugOutput:
            self.printDebug(conn, id, 'QueryIns', 'result')
        return id

    def _queryAddLimitOffset(self, query, start, end):
        if not start:
            return "%s LIMIT %i" % (query, end)
        if not end:
            return "%s LIMIT %i, -1" % (query, start)
        return "%s LIMIT %i, %i" % (query, start, end-start)

    def createColumn(self, soClass, col):
        return col.mysqlCreateSQL()

    def createIDColumn(self, soClass):
        return '%s INT PRIMARY KEY AUTO_INCREMENT' % soClass._idName

    def joinSQLType(self, join):
        return 'INT NOT NULL'

    def tableExists(self, tableName):
        for (table,) in self.queryAll('SHOW TABLES'):
            if table.lower() == tableName.lower():
                return True
        return False

    def addColumn(self, tableName, column):
        self.query('ALTER TABLE %s ADD COLUMN %s' %
                   (tableName,
                    column.mysqlCreateSQL()))

    def delColumn(self, tableName, column):
        self.query('ALTER TABLE %s DROP COLUMN %s' %
                   (tableName,
                    column.dbName))

    def columnsFromSchema(self, tableName, soClass):
        colData = self.queryAll("SHOW COLUMNS FROM %s"
                                % tableName)
        results = []
        for field, t, nullAllowed, key, default, extra in colData:
            if field == 'id':
                continue
            colClass, kw = self.guessClass(t)
            kw['name'] = soClass._style.dbColumnToPythonAttr(field)
            kw['notNone'] = not nullAllowed
            kw['default'] = default
            # @@ skip key...
            # @@ skip extra...
            results.append(colClass(**kw))
        return results

    def guessClass(self, t):
        if t.startswith('int'):
            return col.IntCol, {}
        elif t.startswith('varchar'):
            return col.StringCol, {'length': int(t[8:-1])}
        elif t.startswith('char'):
            return col.StringCol, {'length': int(t[5:-1]),
                                   'varchar': False}
        elif t.startswith('datetime'):
            return col.DateTimeCol, {}
        elif t.startswith('bool'):
            return col.BoolCol, {}
        else:
            return col.Col, {}

