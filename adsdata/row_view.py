
from sqlalchemy import Column, Integer, Float, String, DateTime, Boolean
from sqlalchemy import Table, bindparam, MetaData
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine
from sqlalchemy.sql import select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.schema import CreateSchema, DropSchema
import sys
import argparse

from adsputils import load_config, setup_logging


Base = declarative_base()


class SqlSync:
    """manages 12 fields of nonbibliographic data
    
    Each nonbib data file is ingested from flat/column files to simple tables 
    using Postgres' efficient COPY tablename FROM PROGRAM.
    These tables are joined to create a unified row view of nonbib data.
    
    We use Postgres schemas so we can use sql to compare one set of ingested data against another.
    """

    all_types = ('canonical', 'author', 'refereed', 'simbad', 'grants', 'citation', 'relevance',
                  'reader', 'download', 'reference', 'reads', 'ned')

    def __init__(self, schema_name, passed_config=None):
        """create connection to database, init based on config"""
        self.schema = schema_name
        self.config = load_config()
        if passed_config:
            self.config.update(passed_config)
        self.logger = setup_logging('AdsDataSqlSync', level=self.config.get('LOG_LEVEL', 'INFO'))
        
        connection_string = self.config.get('INGEST_DATABASE',
                                            'postgresql://postgres@localhost:5432/postgres')
        self.engine = create_engine(connection_string, echo=False)
        self.connection = self.engine.connect()
        self.meta = MetaData()
        self.table = self.get_row_view_table()
        
        # sql command to update row with new values, must bind on tmp_bibcode
        self.updater_sql = self.table.update().where(self.table.c.bibcode == bindparam('tmp_bibcode')). \
            values ({'bibcode': bindparam('bibcode'),
                     'id': bindparam('id'),
                     'authors': bindparam('authors'),
                     'refereed': bindparam('refereed'),
                     'simbad_objects': bindparam('simbad_objects'),
                     'ned_objects': bindparam('ned_objects'),
                     'grants': bindparam('grants'),
                     'citations': bindparam('citations'),
                     'boost': bindparam('boost'),
                     'citation_count': bindparam('citation_count'),
                     'read_count': bindparam('read_count'),
                     'norm_cites': bindparam('norm_cites'),
                     'readers': bindparam('readers'),
                     'downloads': bindparam('downloads'),
                     'reads': bindparam('reads'),
                     'reference': bindparam('reference')})


    def create_column_tables(self):
        self.engine.execute(CreateSchema(self.schema))
        temp_meta = MetaData()
        for t in SqlSync.all_types:
            table = self.get_table(t, temp_meta)
        temp_meta.create_all(self.engine)
        self.logger.info('row_view, created database column tables in schema {}'.format(self.schema))
        

    def rename_schema(self, new_name):
        self.engine.execute("alter schema {} rename to {}".format(self.schema, new_name))
        self.logger.info('row_view, renamed schema {} to {} '.format(self.schema, new_name))

    def drop_column_tables(self):
        """drop the entire schema"""
        self.engine.execute("drop schema if exists {} cascade".format(self.schema))
        #temp_meta = MetaData()
        #for t in SqlSync.all_types:
        #    table = self.get_table(t, temp_meta)
        #temp_meta.drop_all(self.engine)
        #self.engine.execute(DropSchema(self.schema))
        self.logger.info('row_view, dropped database column tables in schema {}'.format(self.schema))

    def create_joined_rows(self):
        """join sql tables initialized from the flat/column files into a unified row view"""
        self.logger.info('row_view, creating joined materialized view in schema {}'.format(self.schema))
        Session = sessionmaker()
        sess = Session(bind=self.connection)
        sql_command = SqlSync.create_view_sql.format(self.schema)
        sess.execute(sql_command)
        sess.commit()
        
        sql_command = 'create index on {}.RowViewM (bibcode)'.format(self.schema)
        sess.execute(sql_command)
        sql_command = 'create index on {}.RowViewM (id)'.format(self.schema)
        sess.execute(sql_command)
        sess.commit()
        sess.close()
        self.logger.info('row_view, created joined materialized view in schema {}'.format(self.schema))
    
    def create_delta_rows(self, baseline_schema):
        self.logger.info('row_view, creating delta/changed and new table in schema {}'.format(self.schema))
        Session = sessionmaker()
        sess = Session(bind=self.connection)
        sql_command = SqlSync.create_changed_sql.format(self.schema, baseline_schema)
        sess.execute(sql_command)
        sess.commit()
        sql_command = SqlSync.include_new_bibcodes_sql.format(self.schema, baseline_schema)
        sess.execute(sql_command)
        sess.commit()
        sess.close()
        self.logger.info('row_view, created delta/changed and new table in schema {}'.format(self.schema))
        
        
    def get_delta_table(self, meta=None):
        """ delta table holds list of bibcodes that differ between two row views"""
        if meta is None:
            meta = self.meta
        return Table('changedrowsm', meta,
                     Column('bibcode', String, primary_key=True),
                     schema=self.schema) 

    def get_new_bibcodes_table(self, meta=None):
        """ table holds list of bibcodes that are not in baseline"""
        if meta is None:
            meta = self.meta
        return Table('newbibcodes', meta,
                     Column('bibcode', String, primary_key=True),
                     schema=self.schema)

    def build_new_bibcodes(self, baseline_schema):
        self.logger.info('row_view, dropping and creating new_bibcodes table in schema {}'.format(self.schema))
        self.engine.execute("drop table  if exists {}.newbibcodes;".format(self.schema))
        temp_meta = MetaData()
        table = self.get_new_bibcodes_table(temp_meta)
        temp_meta.create_all(self.engine)
        
        self.logger.info('row_view, created new_bibcodes table in schema {}'.format(self.schema))

        self.logger.info('row_view, populating new_bibcodes table in schame {}'.format(self.schema))
        Session = sessionmaker()
        sess = Session(bind=self.connection)
        sql_command = SqlSync.populate_new_bibcodes_sql.format(self.schema, baseline_schema)
        sess.execute(sql_command)
        sess.commit()
        sess.close()
        self.logger.info('row_view, populated new_bibcodes table in schame {}'.format(self.schema))


        


    def log_delta_reasons(self, baseline_schema):
        """log the counts for the changes in each column from baseline """
        Session = sessionmaker()
        sess = Session(bind=self.connection)
        sql_command = 'select count(*) from ' + self.schema + '.changedrowsm'
        r = sess.execute(sql_command)
        m = 'total number of changed bibcodes: {}'.format(r.scalar())
        print m
        self.logger.info(m)
        
        column_names = ('authors', 'refereed', 'simbad_objects', 'grants', 'citations',
                        'boost', 'citation_count', 'read_count', 'norm_cites',
                        'readers', 'downloads', 'reads', 'reference', 'ned_objects')
        for column_name in column_names:
            sql_command = 'select count(*) from ' + self.schema \
                + '.rowviewm, ' + baseline_schema + '.rowviewm ' \
                + ' where ' + self.schema + '.rowviewm.bibcode=' + baseline_schema + '.rowviewm.bibcode' \
                + ' and ' + self.schema + '.rowviewm.' + column_name + '!=' + baseline_schema + '.rowviewm.' + column_name+ ';'

            r = sess.execute(sql_command)
            m = 'number of {} different: {}'.format(column_name, r.scalar())
            print m
            self.logger.info(m)
        sess.commit()
        sess.close()
        

    def get_canonical_table(self, meta=None):
        if meta is None:
            meta = self.meta
        return Table('canonical', meta,
                     Column('bibcode', String, primary_key=True),
                     Column('id', Integer),
                     schema=self.schema) 

    def get_author_table(self, meta=None):
        if meta is None:
            meta = self.meta
        return Table('author', meta,
                     Column('bibcode', String, primary_key=True),
                     Column('authors', ARRAY(String)),
                     extend_existing=True,
                     schema=self.schema) 

    def get_refereed_table(self, meta=None):
        if meta is None:
            meta = self.meta
        return Table('refereed', meta,
                     Column('bibcode', String, primary_key=True),
                     Column('refereed', Boolean),
                     schema=self.schema)

    def get_simbad_table(self, meta=None):
        if meta is None:
            meta = self.meta
        return Table('simbad', meta,
                     Column('bibcode', String, primary_key=True),
                     Column('simbad_objects', ARRAY(String)),
                     schema=self.schema) 

    def get_ned_table(self, meta=None):
        if meta is None:
            meta = self.meta
        return Table('ned', meta,
                     Column('bibcode', String, primary_key=True),
                     Column('ned_objects', ARRAY(String)),
                     schema=self.schema)

    def get_grants_table(self, meta=None):
        if meta is None:
            meta = self.meta
        return Table('grants', meta,
                     Column('bibcode', String, primary_key=True),
                     Column('grants', ARRAY(String)),
                     schema=self.schema) 

    def get_citation_table(self, meta=None):
        if meta is None:
            meta = self.meta
        return Table('citation', meta,
                     Column('bibcode', String, primary_key=True),
                     Column('citations', ARRAY(String)),
                     schema=self.schema) 

    def get_relevance_table(self, meta=None):
        if meta is None:
            meta = self.meta
        return Table('relevance', meta,
                     Column('bibcode', String, primary_key=True),
                     Column('boost', Float),
                     Column('citation_count', Integer),
                     Column('read_count', Integer),
                     Column('norm_cites', Integer),
                     schema=self.schema) 

    def get_reader_table(self, meta=None):
        if meta is None:
            meta = self.meta
        return Table('reader', meta,
                     Column('bibcode', String, primary_key=True),
                     Column('readers', ARRAY(String)),
                     schema=self.schema) 

    def get_reads_table(self, meta=None):
        if meta is None:
            meta = self.meta
        return Table('reads', meta,
                     Column('bibcode', String, primary_key=True),
                     Column('reads', ARRAY(Integer)),
                     schema=self.schema) 

    def get_download_table(self, meta=None):
        if meta is None:
            meta = self.meta
        return Table('download', meta,
                     Column('bibcode', String, primary_key=True),
                     Column('downloads', ARRAY(Integer)),
                     schema=self.schema) 

    def get_reference_table(self, meta=None):
        if meta is None:
            meta = self.meta
        return Table('reference', meta,
                     Column('bibcode', String, primary_key=True),
                     Column('reference', ARRAY(String)),
                     schema=self.schema) 


    def get_row_view_table(self, meta=None):
        if meta is None:
            meta = self.meta
        return Table('rowviewm', meta,
                     Column('bibcode', String, primary_key=True),
                     Column('id', Integer),
                     Column('authors', ARRAY(String)),
                     Column('refereed', Boolean),
                     Column('simbad_objects', ARRAY(String)),
                     Column('ned_objects', ARRAY(String)),
                     Column('grants', ARRAY(String)),
                     Column('citations', ARRAY(String)),
                     Column('boost', Float),
                     Column('citation_count', Integer),
                     Column('read_count', Integer),
                     Column('norm_cites', Integer),
                     Column('readers', ARRAY(String)),
                     Column('downloads', ARRAY(Integer)),
                     Column('reads', ARRAY(Integer)),
                     Column('reference', ARRAY(String)),
                     schema=self.schema,
                     extend_existing=True)

    def get_changed_rows_table(self, table_name, schema_name, meta=None):
        if meta is None:
            meta = self.meta
        return Table('table_name', meta,
                     Column('bibcode', String, primary_key=True),
                     Column('id', Integer),
                     Column('authors', ARRAY(String)),
                     Column('refereed', Boolean),
                     Column('simbad_objects', ARRAY(String)),
                     Column('ned_objects', ARRAY(String)),
                     Column('grants', ARRAY(String)),
                     Column('citations', ARRAY(String)),
                     Column('boost', Float),
                     Column('citation_count', Integer),
                     Column('read_count', Integer),
                     Column('norm_cites', Integer),
                     Column('readers', ARRAY(String)),
                     Column('downloads', ARRAY(String)),
                     Column('reads', ARRAY(String)),
                     Column('reference', ARRAY(String)),
                     schema=schema_name,
                     extend_existing=True)

    def get_table(self, table_name, meta=None):
        if meta is None:
            meta = self.meta
        method_name = "get_" + table_name + "_table"
        method = getattr(self, method_name)
        table = method(meta)
        return table


    def get_row_view(self, bibcode):
        # connection = engine.connect()
        row_view_select = select([self.table]).where(self.table.c.bibcode == bibcode)
        row_view_result = self.connection.execute(row_view_select)
        first = row_view_result.first()
        return first
    
    def read(self, bibcode):
        """this function is used by utils where a standard name is required"""
        return self.get_row_view(bibcode)

    def get_by_bibcodes(self, bibcodes):
        """ return a list of row view datbase objects matching the list of passed bibcodes"""
        Session = sessionmaker()
        sess = Session(bind=self.connection)
        x = sess.execute(select([self.table], self.table.c.bibcode.in_(bibcodes))).fetchall()
        sess.close()
        return x


    def verify(self, data_dir):
        """verify that the data was properly read in
        we only check files that don't repeat bibcodes, so the
        number of bibcodes equals the number of records
        """
        self.verify_aux(data_dir, '/bibcodes.list.can', self.get_canonical_table())
        self.verify_aux(data_dir, '/facet_authors/all.links', self.get_author_table())
        self.verify_aux(data_dir, '/reads/all.links', self.get_reads_table())
        self.verify_aux(data_dir, '/reads/downloads.links', self.get_download_table())
        self.verify_aux(data_dir, '/refereed/all.links', self.get_refereed_table())
        self.verify_aux(data_dir, '/relevance/docmetrics.tab', self.get_relevance_table())


    def verify_aux(self, data_dir, file_name, sql_table):
        Session = sessionmaker()
        sess = Session(bind=self.connection)
        file = data_dir + file_name
        file_count = self.count_lines(file)
        sql_count = sess.query(sql_table).count()
        if file_count != sql_count:
            print file_name, 'count mismatch:', file_count, sql_count
            return False
        return True

        

    def count_lines(self, file):
        count = 0
        with open(file, "r") as f:
            for line in f:
                count += 1
        return count

    create_view_sql =     \
        'CREATE MATERIALIZED VIEW {0}.RowViewM AS  \
         select bibcode,  \
	      id,         \
              coalesce(authors, ARRAY[]::text[]) as authors,    \
              coalesce(refereed, FALSE) as refereed,            \
              coalesce(simbad_objects, ARRAY[]::text[]) as simbad_objects,  \
              coalesce(ned_objects, ARRAY[]::text[]) as ned_objects,  \
              coalesce(grants, ARRAY[]::text[]) as grants,      \
              coalesce(citations, ARRAY[]::text[]) as citations,\
              coalesce(boost, 0) as boost,                      \
              coalesce(citation_count, 0) as citation_count,    \
              coalesce(read_count, 0) as read_count,            \
              coalesce(norm_cites, 0) as norm_cites,            \
              coalesce(readers, ARRAY[]::text[]) as readers,    \
              coalesce(downloads, ARRAY[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]) as downloads, \
              coalesce(reference, ARRAY[]::text[]) as reference, \
              coalesce(reads, ARRAY[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]) as reads \
       from {0}.Canonical natural left join {0}.Author \
       natural left join {0}.Refereed                 \
       natural left join {0}.Simbad natural left join {0}.Grants \
       natural left join {0}.Citation  natural left join {0}.Ned   \
       natural left join {0}.Relevance natural left join {0}.Reader \
       natural left join {0}.Download natural left join {0}.Reads   \
       natural left join {0}.Reference;' 

    create_changed_sql = \
        'create table {0}.ChangedRowsM as \
         select {0}.RowViewM.bibcode, {0}.RowViewM.id \
         from {0}.RowViewM,{1}.RowViewM \
         where {0}.RowViewM.bibcode={1}.RowViewM.bibcode  \
           and ({0}.RowViewM.authors!={1}.RowViewM.authors \
	   or {0}.RowViewM.refereed!={1}.RowViewM.refereed \
	   or {0}.RowViewM.simbad_objects!={1}.RowViewM.simbad_objects \
           or {0}.RowViewM.ned_objects!={1}.RowViewM.ned_objects \
	   or {0}.RowViewM.grants!={1}.RowViewM.grants \
	   or {0}.RowViewM.citations!={1}.RowViewM.citations \
	   or {0}.RowViewM.boost!={1}.rowViewM.boost \
	   or {0}.RowViewM.norm_cites!={1}.RowViewM.norm_cites \
	   or {0}.RowViewM.citation_count!={1}.RowViewM.citation_count \
	   or {0}.RowViewM.read_count!={1}.RowViewM.read_count \
	   or {0}.RowViewM.readers!={1}.RowViewM.readers \
	   or {0}.RowViewM.downloads!={1}.RowViewM.downloads \
	   or {0}.RowViewM.reads!={1}.RowViewM.reads);'

    # add the new bibcods to the table of changed bibcodes
    include_new_bibcodes_sql = \
        'insert into {0}.ChangedRowsM (bibcode) \
            select {0}.canonical.bibcode from {0}.canonical left join {1}.canonical \
            on {0}.canonical.bibcode = {1}.canonical.bibcode \
            where {1}.canonical.bibcode IS NULL;'

    populate_new_bibcodes_sql = \
        'insert into {0}.newbibcodes (bibcode) \
            select {0}.canonical.bibcode from {0}.canonical left join {1}.canonical \
            on {0}.canonical.bibcode = {1}.canonical.bibcode \
            where {1}.canonical.bibcode IS NULL;'



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='verify ingest of column files')
    parser.add_argument('command', help='verify')
    parser.add_argument('-rowViewSchema', default='ingest', help='schema for column tables')
    parser.add_argument('-dataDir', help='directory for column files', )
    args = parser.parse_args()
    if args.command == 'verify':
        if not args.dataDir:
            print 'argument -dataDir required'
            sys.exit(2)
        row_view = SqlSync(args.rowViewSchema)
        verify = row_view.verify(args.dataDir)
        if verify:
            sys.exit(0)
        sys.exit(1)
    if args.command == 'downloads':
        row_view = SqlSync(args.rowViewSchema)
        Session = sessionmaker()
        sess = Session(bind=row_view.connection)
        t = row_view.get_download_table()
        count = 0
        with open(args.dataDir + '/reads/d1.txt') as f:
            for line in f:
                s = select([t]).where(t.c.bibcode == line.strip())
                rp = row_view.connection.execute(s)
                r = rp.first()
                if r is None:
                    print 'error', line, len(line)
                count += 1
                if count % 1000000 == 0:
                    print count
        print 'count = ', count

                
        
