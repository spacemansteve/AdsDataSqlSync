
import sys
import re
import argparse
import os
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql import select

from adsdata import row_view
from adsdata import metrics
from adsdata import reader
from adsputils import load_config, setup_logging
from adsmsg import NonBibRecord

from adsdata.tasks import task_output_results

logger = None
config = {}


def load_column_files(config, sql_sync):
    """ use psycopg.copy_from to data from column file to postgres
    
    after data has been loaded, join to create a unified row view 
    """
    conn = sql_sync.engine.raw_connection()
    cur = conn.cursor()
    
    for t in row_view.SqlSync.all_types:
        table_name = sql_sync.schema + '.' + t
        logger.info('processing {}'.format(table_name))
        filename = config['DATA_PATH'] + config[t.upper()]
        if t == 'canonical':
            r = reader.BibcodeFileReader(filename)
        elif t == 'refereed':
            r = reader.RefereedFileReader(filename) 
        else:
            r = reader.StandardFileReader(t, filename)
        if r:
            cur.copy_from(r, table_name)
            conn.commit()

    cur.close()
    conn.close()
    sql_sync.create_joined_rows()

def load_metrics(m, row_view_schema):
    """
    """
    Session = sessionmaker()
    sess = Session(bind=m.connection)
    command_args = ' populateMetricsTable '  \
                   + ' --rowViewSchemaName ' + row_view_schema \
                   + ' --metricsSchemaName ' + m.schema
    python_command = "'python " + os.path.abspath(__file__) + command_args + "'"
    sql_command = 'copy ' + m.schema + '.metrics'   \
                  + ' from program ' + python_command + ';'
    
    sess.execute(sql_command)
    sess.commit()
    sess.close()

def nonbib_to_master_pipeline(schema):
    """send all nonbib data to queue for delivery to master pipeline"""
    nonbib = row_view.SqlSync(schema)
    connection = nonbib.engine.connect()
    s = select([nonbib.table])
    results = connection.execute(s)
    for current_row in results:
        current_row = dict(current_row)
        logger.debug('Will forward this record: %s', current_row)
        rec = NonBibRecord(**current_row)
        logger.debug("Calling 'app.forward_message' with '%s'", str(rec))
        task_output_results.delay(rec)


def nonbib_delta_to_master_pipeline(schema):
    """send data for changed bibcodes to master pipeline

    the delta talbe is computed by comparing to sets of nonbib data
    perhaps ingested on succesive days"""
    nonbib = row_view.SqlSync(schema)
    connection = nonbib.engine.connect()
    delta_table = nonbib.get_delta_table()
    s = select([delta_table])
    results = connection.execute(s)
    for current_delta in results:
        row = nonbib.get_row_view(current_delta['bibcode'])
        rec = NonBibRecord(**dict(row))
        logger.debug("Calling 'app.forward_message' with '%s'", str(rec))
        task_output_results.delay(rec)

def diagnose():
    """send hard coded nonbib data the master pipeline

    useful for testing to verify connectivity"""

    test_data = {'bibcode': '2003ASPC..295..361M', 'id': 6473150, 'authors': ["Mcdonald, S","Buddha, S"],
                 'refereed': False, 'simbad_objects': [], 'grants': ['g'], 'citations': [], 'boost': 0.11,
                 'citation_count': 0, 'read_count': 2, 'readers': ['a', 'b'],
                 'downloads': [0,0,0,0,0,0,0,0,0,0,0,1,2,1,0,0,1,0,0,0,1,2],
                 'reference': ['c', 'd'], 
                 'reads': [0,0,0,0,0,0,0,0,1,0,4,2,5,1,0,0,1,0,0,2,4,5]}
    # check readers!
    rec = NonBibRecord(**test_data)
    print 'sending nonbib data for bibocde', test_data['bibcode'], 'to master pipeline'
    print 'using CELERY_BROKER', config['CELERY_BROKER']
    print '  CELERY_DEFAULT_EXCHANGE', config['CELERY_DEFAULT_EXCHANGE']
    print '  CELERY_DEFAULT_EXCHANGE_TYPE', config['CELERY_DEFAULT_EXCHANGE_TYPE']
    print '  OUTPUT_CELERY_BROKER', config['OUTPUT_CELERY_BROKER']
    print '  OUTPUT_TASKNAME', config['OUTPUT_TASKNAME']
    print 'this action did not use ingest database (configured at', config['INGEST_DATABASE'], ')'
    print '  or the metrics database (at', config['METRICS_DATABASE'], ')'
    task_output_results.delay(rec)

    
def main():
    parser = argparse.ArgumentParser(description='process column files into Postgres')
    parser.add_argument('--fileType', default=None, help='all,downloads,simbad,etc.')
    parser.add_argument('-r', '--rowViewSchemaName', default='nonbib', help='name of the postgres row view schema')
    parser.add_argument('-m', '--metricsSchemaName', default='metrics', help='name of the postgres metrics schema')
    parser.add_argument('-b', '--rowViewBaselineSchemaName', default='nonbibstaging', 
                        help='name of old postgres schema, used to compute delta')
    parser.add_argument('-d', '--diagnose', default=False, action='store_true', help='run simple test')
    parser.add_argument('command', default='help', 
                        help='ingest | verify | createIngestTables | dropIngestTables | renameSchema ' \
                        + ' | createJoinedRows | ingestMeta | createMetricsTable | dropMetricsTable ' \
                        + ' | populateMetricsTable | populateMetricsTableMeta | createDeltaRows | populateMetricsTableDelta ' \
                        + ' | runRowViewPipeline | runMetricsPipeline | createNewBibcodes ' \
                        + ' | runRowViewPipelineDelta | runMetricsPipelineDelta '\
                        + ' | runPipelines | runPipelinesDelta | nonbibToMasterPipeline | nonbibDeltaToMasterPipeline')

    args = parser.parse_args()

    config.update(load_config())

    global logger
    logger = setup_logging('AdsDataSqlSync', config.get('LOG_LEVEL', 'INFO'))
    logger.info('starting AdsDataSqlSync.app with {}, {}'.format(args.command, args.fileType))

    if args.command == 'createIngestTables':
        sql_sync = row_view.SqlSync(args.rowViewSchemaName, config)
        sql_sync.create_column_tables()
    elif args.command == 'dropIngestTables':
        sql_sync = row_view.SqlSync(args.rowViewSchemaName, config)
        sql_sync.drop_column_tables()
    elif args.command == 'createJoinedRows':
        sql_sync = row_view.SqlSync(args.rowViewSchemaName, config)
        sql_sync.create_joined_rows()

    elif args.command == 'ingestMeta' and args.rowViewSchemaName:
        # run copy from program command
        # that sql command will invoke ingest of each table
        sql_sync = row_view.SqlSync(args.rowViewSchemaName, config)
        Session = sessionmaker()
        sess = Session(bind=sql_sync.connection)
        for t in row_view.SqlSync.all_types:
            command_args = ' ingest --fileType ' + t +   \
                ' --rowViewSchemaName ' + args.rowViewSchemaName
            python_command = "'python " + os.path.abspath(__file__) + command_args + "'"
            sql_command = 'copy ' + args.rowViewSchemaName + '.' + t +  \
                ' from program ' + python_command + ';'

            sess.execute(sql_command)
            sess.commit()

        sess.close()

    elif args.command == 'createMetricsTable' and args.metricsSchemaName:
        m = metrics.Metrics(args.metricsSchemaName)
        m.create_metrics_table()
    elif args.command == 'dropMetricsTable' and args.metricsSchemaName:
        m = metrics.Metrics(args.metricsSchemaName)
        m.drop_metrics_table()
    elif args.command == 'populateMetricsTable' and args.rowViewSchemaName and args.metricsSchemaName:
        # m = metrics.Metrics(None, {'FROM_SCRATCH': False, 'COPY_FROM_PROGRAM': False})
        m = metrics.Metrics(None, {'FROM_SCRATCH': True, 'COPY_FROM_PROGRAM': True})
        m.update_metrics_all(args.rowViewSchemaName)
    elif args.command == 'populateMetricsTableDelta' and args.rowViewSchemaName and args.metricsSchemaName:
        m = metrics.Metrics(args.metricsSchemaName, {'FROM_SCRATCH': False, 'COPY_FROM_PROGRAM': False})
        m.update_metrics_changed(args.rowViewSchemaName)
    elif args.command == 'populateMetricsTableMeta' and args.rowViewSchemaName and args.metricsSchemaName:
        # run copy from program command
        # that sql command will populate metrics table
        # create metrics to get a database connection for session
        m = metrics.Metrics(args.metricsSchemaName, {'COPY_FROM_PROGRAM': True})
        Session = sessionmaker()
        sess = Session(bind=m.connection)

        command_args = ' populateMetricsTable '  \
            + ' --rowViewSchemaName ' + args.rowViewSchemaName \
            + ' --metricsSchemaName ' + args.metricsSchemaName
        python_command = "'python " + os.path.abspath(__file__) + command_args + "'"
        sql_command = 'copy ' + args.metricsSchemaName + '.metrics'   \
            + ' from program ' + python_command + ';'

        sess.execute(sql_command)
        sess.commit()
        sess.close()
    elif args.command == 'renameSchema' and args.rowViewSchemaName and args.rowViewBaselineSchemaName:
        sql_sync = row_view.SqlSync(args.rowViewSchemaName, config)
        sql_sync.rename_schema(args.rowViewBaselineSchemaName)
    elif args.command == 'createDeltaRows' and args.rowViewSchemaName and args.rowViewBaselineSchemaName:
        sql_sync = row_view.SqlSync(args.rowViewSchemaName, config)
        sql_sync.create_delta_rows(args.rowViewBaselineSchemaName)
    elif args.command == 'createNewBibcodes' and args.rowViewSchemaName and args.rowViewBaselineSchemaName:
        sql_sync = row_view.SqlSync(args.rowViewSchemaName, config)
        sql_sync.build_new_bibcodes(args.rowViewBaselineSchemaName)
    elif args.command == 'logDeltaReasons' and args.rowViewSchemaName and args.rowViewBaselineSchemaName:
        sql_sync = row_view.SqlSync(args.rowViewSchemaName, config)
        sql_sync.log_delta_reasons(args.rowViewBaselineSchemaName)
    elif args.command == 'test':
        sql_sync = row_view.SqlSync(args.rowViewSchemaName, config)
        sql_sync.drop_column_tables()
        sql_sync.create_column_tables()
        test_load_column_files(sql_sync)
    elif args.command == 'runRowViewPipeline' and args.rowViewSchemaName:
        # drop tables, create tables, load data, compute metrics
        sql_sync = row_view.SqlSync(args.rowViewSchemaName, config)
        sql_sync.drop_column_tables()
        sql_sync.create_column_tables()
        load_column_files(config, sql_sync)

    elif args.command == 'runMetricsPipeline' and args.rowViewSchemaName and args.metricsSchemaName:
        m = metrics.Metrics(args.metricsSchemaName, {'COPY_FROM_PROGRAM': True})
        m.drop_metrics_table()
        m.create_metrics_table()
        load_metrics(m, args.rowViewSchemaName)


    elif args.command == 'runRowViewPipelineDelta' and args.rowViewSchemaName and args.rowViewBaselineSchemaName:
        # read in flat files, compare to staging/baseline
        sql_sync = row_view.SqlSync(args.rowViewSchemaName, config)

        sql_sync = row_view.SqlSync(args.rowViewSchemaName, config)
        sql_sync.drop_column_tables()
        sql_sync.create_column_tables()
       
        load_column_files(config, sql_sync)

        sql_sync.create_delta_rows(args.rowViewBaselineSchemaName)
        sql_sync.log_delta_reasons(args.rowViewBaselineSchemaName)

    elif args.command == 'runMetricsPipelineDelta' and args.rowViewSchemaName and args.metricsSchemaName:

        m = metrics.Metrics(args.metricsSchemaName, {'FROM_SCRATCH': False, 'COPY_FROM_PROGRAM': False})
        m.update_metrics_changed(args.rowViewSchemaName)


    elif args.command == 'runPipelines' and args.rowViewSchemaName and args.metricsSchemaName:
        # drop tables, create tables, load data, compute metrics
        sql_sync = row_view.SqlSync(args.rowViewSchemaName, config)
        sql_sync.drop_column_tables()
        sql_sync.create_column_tables()
        load_column_files(config, sql_sync)

        m = metrics.Metrics(args.metricsSchemaName, {'COPY_FROM_PROGRAM': True})
        m.drop_metrics_table()
        m.create_metrics_table()
        load_metrics(m, args.rowViewSchemaName)


    elif args.command == 'runPipelinesDelta' and args.rowViewSchemaName and args.metricsSchemaName and args.rowViewBaselineSchemaName:
        # drop tables, rename schema, create tables, load data, compute delta, compute metrics
        baseline_sql_sync = row_view.SqlSync(args.rowViewBaselineSchemaName, config)
        baseline_sql_sync.drop_column_tables()
        sql_sync = row_view.SqlSync(args.rowViewSchemaName, config)
        sql_sync.rename_schema(args.rowViewBaselineSchemaName)

        baseline_sql_sync = None
        sql_sync = row_view.SqlSync(args.rowViewSchemaName, config)
        sql_sync.create_column_tables()
       
        load_column_files(config, sql_sync)

        sql_sync.create_delta_rows(args.rowViewBaselineSchemaName)
        sql_sync.log_delta_reasons(args.rowViewBaselineSchemaName)

        m = metrics.Metrics(args.metricsSchemaName, {'FROM_SCRATCH': False, 'COPY_FROM_PROGRAM': False})
        m.update_metrics_changed(args.rowViewSchemaName)
    elif args.command == 'nonbibToMasterPipeline' and args.diagnose:
        diagnose()
    elif args.command == 'nonbibToMasterPipeline':
        nonbib_to_master_pipeline(args.rowViewSchemaName)
    elif args.command == 'nonbibDeltaToMasterPipeline':
        print 'diagnose = ', args.diagnose
        nonbib_delta_to_master_pipeline(args.rowViewSchemaName)

    else:
        print 'app.py: illegal command or missing argument, command = ', args.command
        print '  row view schema name = ', args.rowViewSchemaName
        print '  row view baseline schema name = ', args.rowViewBaselineSchemaName
        print '  metrics schema name = ', args.metricsSchemaName

            
    logger.info('completed columnFileIngest with {}, {}'.format(args.command, args.fileType))


if __name__ == "__main__":
    main()
