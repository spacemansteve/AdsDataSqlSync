

# possible values: WARN, INFO, DEBUG
LOGGING_LEVEL = 'DEBUG'

DATA_PATH = '/inputDataDir/current/'
TEST_DATA_PATH = 'tests/data/data1/'

# where to read column files into
INGEST_DATABASE = 'postgresql://postgres@localhost:5432/postgres'
# metrics database used during ingest, not by celery code
METRICS_DATABASE = 'postgresql://postgres@localhost:5432/postgres'


# filenames for column files
DOWNLOAD = 'reads/downloads.links'
READS = 'reads/all.links'
RELEVANCE = 'relevance/docmetrics.tab'
AUTHOR = 'facet_authors/all.links'
#AUTHOR = 'authors/all.links'
REFERENCE = 'reference/all.links'
SIMBAD = 'simbad/simbad_objects.tab'
NED = 'ned/ned_objects.tab' 
GRANTS = 'grants/all.links'
CITATION = 'citation/all.links'
READER = 'alsoread_bib/all.links'
REFEREED = 'refereed/all.links'
CANONICAL = 'bibcodes.list.can'

# number of rows of column file to process
# set to a small number during testing to ingest just a little data quickly
# -1 means process all rows
MAX_ROWS = -1


# ================= celery/rabbitmq rules============== #
# ##################################################### #

ACKS_LATE=True
PREFETCH_MULTIPLIER=1
CELERYD_TASK_SOFT_TIME_LIMIT = 60

CELERY_DEFAULT_EXCHANGE = 'ads-data'
CELERY_DEFAULT_EXCHANGE_TYPE = "topic"

CELERY_BROKER = 'pyamqp://guest:guest@localhost:5682/data_pipeline'
OUTPUT_CELERY_BROKER = 'pyamqp://guest:guest@localhost:5682/master_pipeline'
OUTPUT_TASKNAME = 'adsmp.tasks.task_update_record'







