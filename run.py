
import sys
import re
import argparse
import os
from sqlalchemy.orm import sessionmaker, load_only
from sqlalchemy.sql import select
from sqlalchemy import create_engine

from adsdata import nonbib
from adsdata import metrics
from adsdata import reader
from adsdata import models
from adsputils import load_config, setup_logging
from adsmsg import NonBibRecord, NonBibRecordList, MetricsRecord, MetricsRecordList
from adsdata.tasks import task_output_results, task_output_metrics

from confluent_kafka import Producer

logger = None
config = {}
# fields needed from nonbib to compute master record
nonbib_to_master_select_fields = ('bibcode', 'ads_openaccess', 'author_openaccess', 'boost', 'citation_count',
                                  'eprint_openaccess', 'grants', 'ned_objects', 'nonarticle', 'norm_cites', 'ocrabstract',
                                  'openaccess', 'private', 'pub_openaccess', 'read_count', 'readers', 'reference', 
                                  'refereed', 'simbad_objects', 'toc')

# select fields that are not sent to master, they are used to compute solr property field
nonbib_to_master_property_fields = ('ads_openaccess', 'author_openaccess', 'eprint_openaccess', 
                                    'nonarticle', 'ocrabstract', 'openaccess', 'private', 'pub_openaccess', 
                                    'refereed', 'toc')


def load_column_files(config):
    for t in nonbib.NonBib.all_types:
        #filename = config['DATA_PATH'] + config[t.upper()]
        filename = '/Users/SpacemanSteve/tmp/data/columnFiles/current.20170706/bibcodes.list.can'
        if t == 'canonical':
            p = Producer({'bootstrap.servers': 'localhost:9092',
                          'retries': 0,
                          'batch.num.messages': 16384,
                          'linger.ms': 1})
            r = reader.BibcodeFileReader(filename)
            line = r.readline()
            c = 0
            while len(line) >= 10:
                # p.produce('test', key='20180815', value=line)
                line = r.readline()
                line = line.split()[0]
                c += 1
                if c % 10000 == 0:
                    p.flush(1)
                if c % 1000000 == 0:
                    print '...{}, {}'.format(line, len(line))
            p.flush(30)
            print 'count {} lines to kafka topic'.format(c)


def load_column_files_sql(config, nonbib_db_engine, nonbib_db_conn, sql_sync):
    """ use psycopg.copy_from to data from column file to postgres
    
    after data has been loaded, join to create a unified row view 
    """
    raw_conn = nonbib_db_engine.raw_connection()
    cur = raw_conn.cursor()
    
    for t in nonbib.NonBib.all_types:
        table_name = sql_sync.schema + '.' + t
        logger.info('processing {}'.format(table_name))
        # here we have to read multiple files, information in config file are in a list
        if t == 'datalinks':
            load_column_files_datalinks_table(config[t.upper()], table_name, t, raw_conn, cur)
        else:
            filename = config['DATA_PATH'] + config[t.upper()]
            if t == 'canonical':
                r = reader.BibcodeFileReader(filename)
            elif t in ('refereed', 'pub_openaccess', 'toc', 'private', 'ocrabstract', 'nonarticle'):
                r = reader.OnlyTrueFileReader(filename)
            else:
                r = reader.StandardFileReader(t, filename)
            if r:
                cur.copy_from(r, table_name)
                raw_conn.commit()

    cur.close()
    raw_conn.close()
    sql_sync.create_joined_rows(nonbib_db_conn)


def load_column_files_datalinks_table(from_config, table_name, file_type, raw_conn, cur):

    # from_config is a list of lines that could have one the following two formats
    # path,link_type,link_sub_type (i.e., config/links/eprint_html/all.links,ARTICLE,EPRINT_HTML) or
    # path,link_type (i.e., config/links/video/all.links,PRESENTATION)
    for oneLinkType in from_config:
        if (oneLinkType.count(',') == 1):
            [filename, linktype] = oneLinkType.split(',')
            linksubtype = 'NA'
        elif (oneLinkType.count(',') == 2):
            [filename, linktype, linksubtype] = oneLinkType.split(',')
        else:
            return

        if linktype == 'ASSOCIATED':
            r = reader.DataLinksWithTitleFileReader(file_type, config['DATA_PATH'] + filename, linktype)
        elif linktype == 'DATA':
            r = reader.DataLinksWithTargetFileReader(file_type, config['DATA_PATH'] + filename, linktype)
        else:
            r = reader.DataLinksFileReader(file_type, config['DATA_PATH'] + filename, linktype, linksubtype)

        if r:
            cur.copy_from(r, table_name)
            raw_conn.commit()


def nonbib_to_master_dict(row):
    """create dict using only nonbib fields sent to master in protobuf"""
    d = {}
    for column in nonbib_to_master_select_fields:
        d[column] = getattr(row, column)
    author_count = len(getattr(row, 'authors', ()))
    d['citation_count_norm'] = getattr(row, 'citation_count', 0) / float(max(author_count, 1))
    return d


def row2dict(row):
    """ from https://stackoverflow.com/questions/1958219/convert-sqlalchemy-row-object-to-python-dict"""
    d = {}
    for column in row.__table__.columns:
        d[column.name] = getattr(row, column.name)
    return d


def fetch_data_link_elements(query_result):
    elements = []
    if (query_result[0] != None):
        for e in query_result[0].split(','):
            if (e != None):
                elements.append(e)
    return elements


def fetch_data_link_elements_counts(query_result):
    elements = []
    cumulative_count = 0
    if (query_result[1] != None):
        for e in query_result[1].split(','):
            if (e != None):
                elements.append(e)
        cumulative_count = query_result[0]
    return [elements, cumulative_count]


def fetch_data_link_record(query_result):
    # since I want to use this function from the test side,
    # I was not able to use the elegant function row2dict function
    # convert query results to a list of dicts 
    columns = ['link_type', 'link_sub_type', 'url', 'title', 'item_count']
    results = []
    for row in query_result:
        d = {}
        for i, field in enumerate(row):
            d[columns[i]] = field
        results.append(d)
    return results

def add_data_link_extra_properties(current_row):
    # first, augment property field with article/nonartile, refereed/not refereed
    if current_row['nonarticle']:
        current_row['property'].append(u'NONARTICLE')
    else:
        current_row['property'].append(u'ARTICLE')
    if current_row['refereed']:
        current_row['property'].append(u'REFEREED')
    else:
        current_row['property'].append(u'NOT REFEREED')
    # now augment the property field with many other boolean fields
    extra_properties = ('pub_openaccess', 'toc', 'private', 'ocrabstract')
    for p in extra_properties:
        if current_row[p]:
            current_row['property'].append(p.upper())
    # these property fields are set from availability of url
    extra_properties_link_type = {'ADS_PDF':'ADS_OPENACCESS', 'ADS_SCAN':'ADS_OPENACCESS',
                                  'AUTHOR_PDF':'AUTHOR_OPENACCESS', 'AUTHOR_HTML':'AUTHOR_OPENACCESS',
                                  'EPRINT_PDF':'EPRINT_OPENACCESS', 'EPRINT_HTML':'EPRINT_OPENACCESS'}
    for key,value in extra_properties_link_type.iteritems():
        if key in current_row['esource']:
            current_row['property'].append(value)
    # see if there is any of *_openaccess flags set, if so set the generic openaccess flag
    if ('ADS_OPENACCESS' in current_row['property']) or ('AUTHOR_OPENACCESS' in current_row['property']) or \
       ('EPRINT_OPENACCESS' in current_row['property']) or ('ADS_OPENACCESS' in current_row['property']):
       current_row['property'].append('OPENACCESS')
    return current_row

def add_data_link(session, current_row):
    """populate property, esource, data, total_link_counts, and data_links_rows fields"""

    q = config['PROPERTY_QUERY'].format(db='nonbib', bibcode=current_row['bibcode'])
    result = session.execute(q)
    current_row['property'] = fetch_data_link_elements(result.fetchone())

    q = config['ESOURCE_QUERY'].format(db='nonbib', bibcode=current_row['bibcode'])
    result = session.execute(q)
    current_row['esource'] = fetch_data_link_elements(result.fetchone())

    current_row = add_data_link_extra_properties(current_row)

    q = config['DATA_QUERY'].format(db='nonbib', bibcode=current_row['bibcode'])
    result = session.execute(q)
    current_row['data'],current_row['total_link_counts'] = fetch_data_link_elements_counts(result.fetchone())

    q = config['DATALINKS_QUERY'].format(db='nonbib', bibcode=current_row['bibcode'])
    result = session.execute(q)
    current_row['data_links_rows'] = fetch_data_link_record(result.fetchall())


def cleanup_for_master(r):
    """delete values from dict not needed by protobuf to master pipeline"""
    for f in nonbib_to_master_property_fields:
        r.pop(f, None)


def nonbib_to_master_pipeline(nonbib_engine, schema, batch_size=1):
    """send all nonbib data to queue for delivery to master pipeline"""
    global config
    Session = sessionmaker(bind=nonbib_engine)
    session = Session()
    session.execute('set search_path to {}'.format(schema))
    tmp = []
    i = 0
    max_rows = config['MAX_ROWS']
    q = session.query(models.NonBibTable).options(load_only(*nonbib_to_master_select_fields))
    for current_row in q.yield_per(100):
        current_row = nonbib_to_master_dict(current_row)
        add_data_link(session, current_row)
        cleanup_for_master(current_row)
        rec = NonBibRecord(**current_row)
        tmp.append(rec._data)
        i += 1
        if max_rows > 0 and i >= max_rows:
            break
        if len(tmp) >= batch_size:
            recs = NonBibRecordList()
            recs.nonbib_records.extend(tmp)
            tmp = []
            logger.info("Calling 'app.forward_message' count = '%s'", i)
            task_output_results.delay(recs)

    if len(tmp) > 0:
        recs = NonBibRecordList()
        recs.nonbib_records.extend(tmp)
        logger.info("Calling 'app.forward_message' with count = '%s'", i)
        task_output_results.delay(recs)
    session.close()


def nonbib_delta_to_master_pipeline(nonbib_engine, schema, batch_size=1):
    """send data for changed bibcodes to master pipeline

    the delta table was computed by comparing to sets of nonbib data
    perhaps ingested on succesive days"""
    global config
    Session = sessionmaker(bind=nonbib_engine)
    session = Session()
    session.execute('set search_path to {}'.format(schema))
    tmp = []
    i = 0
    n = nonbib.NonBib(schema)
    max_rows = config['MAX_ROWS']
    for current_delta in session.query(models.NonBibDeltaTable).yield_per(100):
        row = n.get_by_bibcode(nonbib_engine, current_delta.bibcode, nonbib_to_master_select_fields)
        row = nonbib_to_master_dict(row)
        add_data_link(session, row)
        cleanup_for_master(row)
        rec = NonBibRecord(**row)
        tmp.append(rec._data)
        i += 1
        if max_rows > 0 and i > max_rows:
            break
        if len(tmp) >= batch_size:
            recs = NonBibRecordList()
            recs.nonbib_records.extend(tmp)
            tmp = []
            logger.debug("Calling 'app.forward_message' with '%s' items", len(recs.nonbib_records))
            task_output_results.delay(recs)

    if len(tmp) > 0:
        recs = NonBibRecordList()
        recs.nonbib_records.extend(tmp)
        logger.debug("Calling 'app.forward_message' with final '%s' items", len(recs.nonbib_records))
        task_output_results.delay(recs)


def nonbib_bibs_to_master_pipeline(nonbib_engine, schema, bibcodes):
    """send data for the passed bibcodes to master"""
    Session = sessionmaker(bind=nonbib_engine)
    session = Session()
    session.execute('set search_path to {}'.format(schema))
    n = nonbib.NonBib(schema)
    tmp = [] 
    for bibcode in bibcodes:
        row = n.get_by_bibcode(nonbib_engine, bibcode, nonbib_to_master_select_fields)
        if row:
            row = nonbib_to_master_dict(row)
            add_data_link(session, row)
            cleanup_for_master(row)
            rec = NonBibRecord(**row)
            tmp.append(rec._data)
        else:
            print 'unknown bibcode ', bibcode
    recs = NonBibRecordList()
    recs.nonbib_records.extend(tmp)
    logger.debug("Calling 'app.forward_message' for '%s' bibcodes", len(recs.nonbib_records))
    task_output_results.delay(recs)


def metrics_to_master_pipeline(metrics_engine, schema, batch_size=1):
    """send all metrics data to queue for delivery to master pipeline"""
    global config
    Session = sessionmaker(bind=metrics_engine)
    session = Session()
    session.execute('set search_path to {}'.format(schema))
    tmp = []
    i = 0
    max_rows = config['MAX_ROWS']
    for current_row in session.query(models.MetricsTable).yield_per(100):
        current_row = row2dict(current_row)
        current_row.pop('id')
        rec = MetricsRecord(**current_row)
        tmp.append(rec._data)
        i += 1
        if max_rows > 0 and i > max_rows:
            break
        if len(tmp) >= batch_size:
            recs = MetricsRecordList()
            recs.metrics_records.extend(tmp)
            logger.info("Calling metrics 'app.forward_message' count = '%s'", i)
            task_output_metrics.delay(recs)
            tmp = []

    if len(tmp) > 0:
        recs = MetricsRecordList()
        recs.metrics_records.extend(tmp)
        logger.debug("Calling metrics 'app.forward_message' with count = '%s'", i)
        task_output_metrics.delay(recs)


def metrics_delta_to_master_pipeline(metrics_engine, metrics_schema, nonbib_engine, nonbib_schema,  batch_size=1):
    """send data for changed metrics to master pipeline

    the delta table was computed by comparing to sets of nonbib data
    perhaps ingested on succesive days"""
    global config
    Nonbib_Session = sessionmaker(bind=nonbib_engine)
    nonbib_session = Nonbib_Session()
    nonbib_session.execute('set search_path to {}'.format(nonbib_schema))

    Metrics_Session = sessionmaker(bind=metrics_engine)
    metrics_session = Metrics_Session()
    metrics_session.execute('set search_path to {}'.format(metrics_schema))

    m = metrics.Metrics(metrics_schema)
    n = nonbib.NonBib(nonbib_schema)
    max_rows = config['MAX_ROWS']
    tmp = []
    i = 0
    for current_delta in nonbib_session.query(models.NonBibDeltaTable).yield_per(100):
        row = m.get_by_bibcode(metrics_session, current_delta.bibcode)
        rec = row2dict(row)
        rec.pop('id')
        rec = MetricsRecord(**dict(rec))
        tmp.append(rec._data)
        i += 1
        if max_rows > 0 and i > max_rows:
            break
        if len(tmp) >= batch_size:
            recs = MetricsRecordList()
            recs.metrics_records.extend(tmp)
            logger.debug("Calling metrics 'app.forward_message' with '%s' messages", len(recs.metrics_records))
            task_output_metrics.delay(recs)
            tmp = []

    if len(tmp) > 0:
        recs = MetricsRecordList()
        recs.metrics_records.extend(tmp)
        logger.debug("Calling metrics 'app.forward_message' with final '%s'", str(rec))
        task_output_metrics.delay(recs)

def metrics_bibs_to_master_pipeline(metrics_engine, metrics_schema, bibcodes):
    """send the passed list of bibcodes to master"""
    Metrics_Session = sessionmaker(bind=metrics_engine)
    metrics_session = Metrics_Session()
    metrics_session.execute('set search_path to {}'.format(metrics_schema))
    tmp = []
    m = metrics.Metrics(metrics_schema)
    for bibcode in bibcodes:
        row = m.get_by_bibcode(metrics_session, bibcode)
        if row:
            rec = row2dict(row)
            rec.pop('id')
            rec = MetricsRecord(**dict(rec))
            tmp.append(rec._data)
        else:
            print 'unknown bibcode: ', bibcode
        
    recs = MetricsRecordList()
    recs.metrics_records.extend(tmp)
    logger.debug("Calling metrics 'app.forward_message' for '%s' bibcodes", str(rec))
    task_output_metrics.delay(recs)
        
    



def diagnose_nonbib():
    """send hard coded nonbib data the master pipeline

    useful for testing to verify connectivity"""

    test_data = {'bibcode': '2003ASPC..295..361M',
                 'simbad_objects': [], 'grants': ['g'], 'boost': 0.11,
                 'citation_count': 0, 'read_count': 2, 'readers': ['a', 'b'],
                 'reference': ['c', 'd']}
    recs = NonBibRecordList()
    rec = NonBibRecord(**test_data)
    recs.nonbib_records.extend([rec._data])
    print 'sending nonbib data for bibocde', test_data['bibcode'], 'to master pipeline'
    print 'using CELERY_BROKER', config['CELERY_BROKER']
    print '  CELERY_DEFAULT_EXCHANGE', config['CELERY_DEFAULT_EXCHANGE']
    print '  CELERY_DEFAULT_EXCHANGE_TYPE', config['CELERY_DEFAULT_EXCHANGE_TYPE']
    print '  OUTPUT_CELERY_BROKER', config['OUTPUT_CELERY_BROKER']
    print '  OUTPUT_TASKNAME', config['OUTPUT_TASKNAME']
    print 'this action did not use ingest database (configured at', config['INGEST_DATABASE'], ')'
    print '  or the metrics database (at', config['METRICS_DATABASE'], ')'
    task_output_results.delay(recs)


def diagnose_metrics():
    """send hard coded metrics data the master pipeline

    useful for testing to verify connectivity"""

    test_data = {'bibcode': '2003ASPC..295..361M', 'refereed':False, 'rn_citations': 0, 
                 'rn_citation_data': [], 
                 'downloads': [0,0,0,0,0,0,0,0,0,0,0,1,2,1,0,0,1,0,0,0,1,2],
                 'reads': [0,0,0,0,0,0,0,0,1,0,4,2,5,1,0,0,1,0,0,2,4,5], 
                 'an_citations': 0, 'refereed_citation_num': 0,
                 'citation_num': 0, 'reference_num': 0, 'citations': [], 'refereed_citations': [], 
                 'author_num': 2, 'an_refereed_citations': 0
                 }
    recs = MetricsRecordList()
    rec = MetricsRecord(**test_data)
    recs.metrics_records.extend([rec._data])
    print 'sending metrics data for bibocde', test_data['bibcode'], 'to master pipeline'
    print 'using CELERY_BROKER', config['CELERY_BROKER']
    print '  CELERY_DEFAULT_EXCHANGE', config['CELERY_DEFAULT_EXCHANGE']
    print '  CELERY_DEFAULT_EXCHANGE_TYPE', config['CELERY_DEFAULT_EXCHANGE_TYPE']
    print '  OUTPUT_CELERY_BROKER', config['OUTPUT_CELERY_BROKER']
    print '  OUTPUT_TASKNAME', config['OUTPUT_TASKNAME']
    print 'this action did not use ingest database (configured at', config['INGEST_DATABASE'], ')'
    print '  or the metrics database (at', config['METRICS_DATABASE'], ')'
    task_output_metrics.delay(recs)



    
def main():
    parser = argparse.ArgumentParser(description='process column files into Postgres')
    parser.add_argument('-t', '--rowViewBaselineSchemaName', default='nonbibstaging', 
                        help='name of old postgres schema, used to compute delta')
    parser.add_argument('-d', '--diagnose', default=False, action='store_true', help='run simple test')
    parser.add_argument('-f', '--filename', default='bibcodes.txt', help='name of file containing the list of bibcode for metrics comparison')
    parser.add_argument('-m', '--metricsSchemaName', default='metrics', help='name of the postgres metrics schema')
    parser.add_argument('-n', '--metricsSchemaName2', default='', help='name of the postgres metrics schema for comparison')
    parser.add_argument('-r', '--rowViewSchemaName', default='nonbib', help='name of the postgres row view schema')
    parser.add_argument('-s', '--batchSize', default=100,  help='used when queuing data')
    parser.add_argument('-b', '--bibcodes', default='',  help='comma separate list of bibcodes send to master pipeline')
    parser.add_argument('command', default='help', nargs='?',
                        help='ingest | verify | createIngestTables | dropIngestTables | renameSchema ' \
                        + ' | createJoinedRows | createMetricsTable | dropMetricsTable ' \
                        + ' | populateMetricsTable | createDeltaRows | populateMetricsTableDelta ' \
                        + ' | runRowViewPipeline | runMetricsPipeline | createNewBibcodes ' \
                        + ' | runRowViewPipelineDelta | runMetricsPipelineDelta '\
                        + ' | runPipelines | runPipelinesDelta | nonbibToMasterPipeline | nonbibDeltaToMasterPipeline'
                        + ' | metricsToMasterPipeline | metricsDeltaToMasterPipeline | metricsCompare')

    args = parser.parse_args()

    config.update(load_config())
    load_column_files(config)
    if True:
        return

    global logger
    logger = setup_logging('AdsDataSqlSync', config.get('LOG_LEVEL', 'INFO'))
    logger.info('starting AdsDataSqlSync.app with {}'.format(args.command))
    nonbib_connection_string = config.get('INGEST_DATABASE',
                                   'postgresql://postgres@localhost:5432/postgres')
    nonbib_db_engine = create_engine(nonbib_connection_string)
    nonbib_db_conn = nonbib_db_engine.connect()

    metrics_connection_string = config.get('METRICS_DATABASE',
                                   'postgresql://postgres@localhost:5432/postgres')
    metrics_db_engine = create_engine(metrics_connection_string)
    metrics_db_conn = metrics_db_engine.connect()
    sql_sync = nonbib.NonBib(args.rowViewSchemaName)
    if args.command == 'help' and args.diagnose:
        diagnose_nonbib()
        diagnose_metrics()

    elif args.command == 'createIngestTables':
        sql_sync.create_column_tables(nonbib_db_engine)

    elif args.command == 'dropIngestTables':
        sql_sync.drop_column_tables(nonbib_db_engine)

    elif args.command == 'createJoinedRows':
        sql_sync.create_joined_rows(nonbib_db_conn)

    elif args.command == 'createMetricsTable' and args.metricsSchemaName:
        m = metrics.Metrics(args.metricsSchemaName)
        m.create_metrics_table(metrics_db_engine)

    elif args.command == 'dropMetricsTable' and args.metricsSchemaName:
        m = metrics.Metrics(args.metricsSchemaName)
        m.drop_metrics_table(metrics_db_engine)

    elif args.command == 'populateMetricsTable' and args.rowViewSchemaName and args.metricsSchemaName:
        m = metrics.Metrics()
        m.update_metrics_all(metrics_db_conn, nonbib_db_conn, args.rowViewSchemaName)

    elif args.command == 'populateMetricsTableDelta' and args.rowViewSchemaName and args.metricsSchemaName:
        m = metrics.Metrics(args.metricsSchemaName)
        m.update_metrics_changed(metrics_db_conn, nonbib_db_conn, args.rowViewSchemaName)

    elif args.command == 'renameSchema' and args.rowViewSchemaName and args.rowViewBaselineSchemaName:
        sql_sync.rename_schema(nonbib_db_conn, args.rowViewBaselineSchemaName)

    elif args.command == 'createDeltaRows' and args.rowViewSchemaName and args.rowViewBaselineSchemaName:
        sql_sync.create_delta_rows(nonbib_db_conn, args.rowViewBaselineSchemaName)

    elif args.command == 'createNewBibcodes' and args.rowViewSchemaName and args.rowViewBaselineSchemaName:
        sql_sync.build_new_bibcodes(nonbib_db_conn, args.rowViewBaselineSchemaName)

    elif args.command == 'logDeltaReasons' and args.rowViewSchemaName and args.rowViewBaselineSchemaName:
        sql_sync.log_delta_reasons(nonbib_db_conn, args.rowViewBaselineSchemaName)

    elif args.command == 'runRowViewPipeline' and args.rowViewSchemaName:
        # drop tables, create tables, load data, create joined view
        sql_sync.drop_column_tables(nonbib_db_engine)
        sql_sync.create_column_tables(nonbib_db_engine)
        load_column_files(config, nonbib_db_engine, nonbib_db_conn, sql_sync)

    elif args.command == 'runMetricsPipeline' and args.rowViewSchemaName and args.metricsSchemaName:
        m = metrics.Metrics(args.metricsSchemaName)
        m.drop_metrics_table(metrics_db_engine)
        m.create_metrics_table(metrics_db_engine)
        m.update_metrics_all(metrics_db_conn, nonbib_db_conn, args.rowViewSchemaName)

    elif args.command == 'runRowViewPipelineDelta' and args.rowViewSchemaName and args.rowViewBaselineSchemaName:
        # we delete the old data
        baseline_sql_sync = nonbib.NonBib(args.rowViewBaselineSchemaName)
        baseline_engine = create_engine(nonbib_connection_string)
        baseline_sql_sync.drop_column_tables(baseline_engine)
        # rename the current to be the old (for later comparison)
        sql_sync.rename_schema(nonbib_db_conn, args.rowViewBaselineSchemaName)
        # create the new and populate
        baseline_sql_sync = None
        sql_sync.create_column_tables(nonbib_db_engine)
        load_column_files(config, nonbib_db_engine, nonbib_db_conn, sql_sync)
        # compute delta between old and new
        sql_sync.create_delta_rows(nonbib_db_conn, args.rowViewBaselineSchemaName)
        sql_sync.log_delta_reasons(nonbib_db_conn, args.rowViewBaselineSchemaName)

    elif args.command == 'runMetricsPipelineDelta' and args.rowViewSchemaName and args.metricsSchemaName:
        m = metrics.Metrics(args.metricsSchemaName)
        m.update_metrics_changed(metrics_db_conn, nonbib_db_conn, args.rowViewSchemaName)

    elif args.command == 'runPipelines' and args.rowViewSchemaName and args.metricsSchemaName:
        # drop tables, create tables, load data, compute metrics
        sql_sync.drop_column_tables(nonbib_db_engine)
        sql_sync.create_column_tables(nonbib_db_engine)
        load_column_files(config, nonbib_db_engine, nonbib_db_conn, sql_sync)

        m = metrics.Metrics(args.metricsSchemaName)
        m.drop_metrics_table(metrics_db_engine)
        m.create_metrics_table(metrics_db_engine)
        m.update_metrics_all(metrics_db_conn, nonbib_db_conn, args.rowViewSchemaName)

    elif args.command == 'runPipelinesDelta' and args.rowViewSchemaName and args.metricsSchemaName and args.rowViewBaselineSchemaName:
        # drop tables, rename schema, create tables, load data, compute delta, compute metrics
        baseline_sql_sync = nonbib.NonBib(args.rowViewBaselineSchemaName)
        baseline_engine = create_engine(nonbib_connection_string)
        baseline_sql_sync.drop_column_tables(baseline_engine)
        sql_sync.rename_schema(nonbib_db_conn, args.rowViewBaselineSchemaName)

        baseline_sql_sync = None
        sql_sync.create_column_tables(nonbib_db_engine)
        load_column_files(config, nonbib_db_engine, nonbib_db_conn, sql_sync)

        sql_sync.create_delta_rows(nonbib_db_conn, args.rowViewBaselineSchemaName)
        sql_sync.log_delta_reasons(nonbib_db_conn, args.rowViewBaselineSchemaName)

        m = metrics.Metrics(args.metricsSchemaName)
        m.update_metrics_changed(metrics_db_conn, nonbib_db_conn, args.rowViewSchemaName)

    elif args.command == 'nonbibToMasterPipeline' and args.diagnose:
        diagnose_nonbib()
    elif args.command == 'nonbibToMasterPipeline' and args.bibcodes:
        bibcodes = args.bibcodes.split(',')
        nonbib_bibs_to_master_pipeline(nonbib_db_engine, args.rowViewSchemaName, bibcodes)
    elif args.command == 'nonbibToMasterPipeline':
        nonbib_to_master_pipeline(nonbib_db_engine, args.rowViewSchemaName, int(args.batchSize))
    elif args.command == 'nonbibDeltaToMasterPipeline':
        nonbib_delta_to_master_pipeline(nonbib_db_engine, args.rowViewSchemaName, int(args.batchSize))
    elif args.command == 'metricsToMasterPipeline' and args.diagnose:
        diagnose_metrics()
    elif args.command == 'metricsToMasterPipeline' and args.bibcodes:
        bibcodes = args.bibcodes.split(',')
        metrics_bibs_to_master_pipeline(metrics_db_engine, args.metricsSchemaName, bibcodes)
    elif args.command == 'metricsToMasterPipeline':
        metrics_to_master_pipeline(metrics_db_engine, args.metricsSchemaName, int(args.batchSize))
    elif args.command == 'metricsDeltaToMasterPipeline':
        metrics_delta_to_master_pipeline(metrics_db_engine, args.metricsSchemaName, nonbib_db_engine, args.rowViewSchemaName, int(args.batchSize))

    elif args.command == 'metricsCompare':
        # compare the values in two metrics postgres tables
        # useful to compare results from new pipeline to produciton pipeline
        # read metrics records from both databases and compare
        metrics_logger = setup_logging('metricsCompare', 'INFO')
        metrics1 = metrics.Metrics(args.metricsSchemaName)
        Session = sessionmaker(bind=metrics_db_engine)
        session = Session()
        if args.metricsSchemaName:
            session.execute('set search_path to {}'.format(args.metricsSchemaName))

        metrics2 = metrics.Metrics(args.metricsSchemaName2)
        metrics_connection_string2 = config.get('METRICS_DATABASE2',
                                               'postgresql://postgres@localhost:5432/postgres')
        metrics_db_engine2 = create_engine(metrics_connection_string2)
        Session2 = sessionmaker(bind=metrics_db_engine2)
        session2 = Session2()
        if args.metricsSchemaName2:
            session2.execute('set search_path to {}'.format(args.metricsSchemaName2))

        print 'm2', metrics_connection_string2
        print 'm2 schema', args.metricsSchemaName2
        with open(args.filename) as f:
            for line in f:
                bibcode = line.strip()
                m1 = metrics1.get_by_bibcode(session, bibcode)
                m2 = metrics2.get_by_bibcode(session2, bibcode)
                mismatch = metrics.Metrics.metrics_mismatch(line.strip(), m1, m2, metrics_logger)
                if mismatch:
                    metrics_logger.error('{} MISMATCHED FIELDS: {}'.format(bibcode, mismatch))
                    print '{} MISMATCHED FIELDS: {}'.format(bibcode, mismatch)
        session.close()
        session2.close()

    else:
        print 'app.py: illegal command or missing argument, command = ', args.command
        print '  row view schema name = ', args.rowViewSchemaName
        print '  row view baseline schema name = ', args.rowViewBaselineSchemaName
        print '  metrics schema name = ', args.metricsSchemaName

    if nonbib_db_conn:
        nonbib_db_conn.close()
    if metrics_db_conn:
        metrics_db_conn.close()
    logger.info('completed {}'.format(args.command))



if __name__ == "__main__":
    main()
