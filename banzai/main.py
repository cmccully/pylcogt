""" main.py: Main driver script for banzai.

    The main() function is a console entry point.

Author
    Curtis McCully (cmccully@lcogt.net)

October 2015
"""
import argparse
import os
import logging
import sys
# import dramatiq

from kombu import Exchange, Connection, Queue
from lcogt_logging import LCOGTFormatter
# from apscheduler.triggers.cron import CronTrigger
# from apscheduler.schedulers.blocking import BlockingScheduler

from banzai import dbs, logs, calibrations
from banzai.context import Context
from banzai.utils import image_utils, date_utils, fits_utils, import_utils
from banzai.celery import schedule_stacking_checks
from banzai.realtime import RealtimeModeListener
from banzai import settings


# Logger set up
logging.captureWarnings(True)
# Set up the root logger
root_logger = logging.getLogger()
root_handler = logging.StreamHandler(sys.stdout)
# Add handler
formatter = LCOGTFormatter()
root_handler.setFormatter(formatter)
root_handler.setLevel(getattr(logging, 'DEBUG'))
root_logger.addHandler(root_handler)

logger = logging.getLogger(__name__)

# dramatiq.set_broker(settings.REDIS_BROKER)
# dramatiq.set_encoder(ContextJSONEncoder())

RAW_PATH_CONSOLE_ARGUMENT = {'args': ["--raw-path"],
                             'kwargs': {'dest': 'raw_path', 'default': '/archive/engineering',
                                        'help': 'Top level directory where the raw data is stored'}}


def get_stages_todo(ordered_stages, last_stage=None, extra_stages=None):
    """

    Parameters
    ----------
    ordered_stages: list of banzai.stages.Stage objects
    last_stage: banzai.stages.Stage
                Last stage to do
    extra_stages: Stages to do after the last stage

    Returns
    -------
    stages_todo: list of banzai.stages.Stage
                 The stages that need to be done

    Notes
    -----
    Extra stages can be other stages that are not in the ordered_stages list.
    """
    if extra_stages is None:
        extra_stages = []

    if last_stage is None:
        last_index = None
    else:
        last_index = ordered_stages.index(last_stage) + 1

    stages_todo = [import_utils.import_attribute(stage) for stage in ordered_stages[:last_index]]

    stages_todo += [import_utils.import_attribute(stage) for stage in extra_stages]

    return stages_todo


def parse_args(extra_console_arguments=None, parser_description='Process LCO data.'):
    """Parse arguments, including default command line argument, and set the overall log level"""

    parser = argparse.ArgumentParser(description=parser_description)

    parser.add_argument("--processed-path", default='/archive/engineering',
                        help='Top level directory where the processed data will be stored')
    parser.add_argument("--log-level", default='debug', choices=['debug', 'info', 'warning',
                                                                 'critical', 'fatal', 'error'])
    parser.add_argument('--post-to-archive', dest='post_to_archive', action='store_true',
                        default=False)
    parser.add_argument('--post-to-elasticsearch', dest='post_to_elasticsearch', action='store_true',
                        default=False)
    parser.add_argument('--fpack', dest='fpack', action='store_true', default=False,
                        help='Fpack the output files?')
    parser.add_argument('--rlevel', dest='rlevel', default=91, type=int, help='Reduction level')
    parser.add_argument('--db-address', dest='db_address',
                        default='mysql://cmccully:password@localhost/test',
                        help='Database address: Should be in SQLAlchemy form')
    parser.add_argument('--elasticsearch-url', dest='elasticsearch_url',
                        default='http://elasticsearch.lco.gtn:9200')
    parser.add_argument('--es-index', dest='elasticsearch_qc_index', default='banzai_qc',
                        help='ElasticSearch index to use for QC results')
    parser.add_argument('--es-doc-type', dest='elasticsearch_doc_type', default='qc',
                        help='Elasticsearch document type for QC records')
    parser.add_argument('--no-bpm', dest='no_bpm', default=False, action='store_true',
                        help='Do not use a bad pixel mask to reduce data (BPM contains all zeros)')
    parser.add_argument('--ignore-schedulability', dest='ignore_schedulability',
                        default=False, action='store_true',
                        help='Relax requirement that the instrument be schedulable')
    parser.add_argument('--use-only-older-calibrations', dest='use_only_older_calibrations', default=False,
                        action='store_true', help='Only use calibrations that were created before the start of the block')
    parser.add_argument('--preview-mode', dest='preview_mode', default=False, action='store_true',
                        help='Save the reductions to the preview directory')
    parser.add_argument('--max-tries', dest='max_tries', default=5,
                        help='Maximum number of times to try to process a frame')

    if extra_console_arguments is None:
        extra_console_arguments = []
    for argument in extra_console_arguments:
        parser.add_argument(*argument['args'], **argument['kwargs'])
    args = parser.parse_args()

    logs.set_log_level(args.log_level)

    runtime_context = Context(args)

    return runtime_context


# def start_schedule_calibration_stacking(runtime_context=None, raw_path=None):
#     runtime_context, raw_path = parse_directory_args(runtime_context, raw_path)
#     scheduler = BlockingScheduler()
#     for site, entry in settings.SCHEDULE_STACKING_CRON_ENTRIES.items():
#         runtime_context_json = dict(runtime_context._asdict())
#         runtime_context_json['site'] = site
#         worker_runtime_context = Context(runtime_context_json)
#         scheduler.add_job(
#             schedule_calibration_stacking.send_with_options(runtime_context=worker_runtime_context,
#                                                             raw_path=raw_path),
#             CronTrigger.from_crontab(entry),
#         )

#     try:
#         scheduler.start()
#     except KeyboardInterrupt:
#         scheduler.shutdown()


def run(image_path, runtime_context):
    """
    Main driver script for banzai.
    """
    image = image_utils.read_image(image_path, runtime_context)
    stages_to_do = get_stages_todo(settings.ORDERED_STAGES,
                                   last_stage=settings.LAST_STAGE[image.obstype],
                                   extra_stages=settings.EXTRA_STAGES[image.obstype])
    logger.info("Starting to reduce frame", image=image)
    for stage in stages_to_do:
        stage_to_run = stage(runtime_context)
        image = stage_to_run.run(image)
    if image is None:
        logger.error('Reduction stopped', extra_tags={'filename': image_path})
        return
    image.write(runtime_context)
    logger.info("Finished reducing frame", image=image)


def process_directory(runtime_context, raw_path, image_types=None, log_message=''):
    if len(log_message) > 0:
        logger.info(log_message, extra_tags={'raw_path': raw_path})
    image_path_list = image_utils.make_image_path_list(raw_path)
    if image_types is None:
        image_types = [None]
    images_to_reduce = []
    for image_type in image_types:
        images_to_reduce += image_utils.select_images(image_path_list, image_type, runtime_context.db_address,
                                                      runtime_context.ignore_schedulability)
    for image_path in images_to_reduce:
        try:
            run(image_path, runtime_context)
        except Exception:
            logger.error(logs.format_exception(), extra_tags={'filename': image_path})


def process_single_frame(runtime_context, raw_path, filename, log_message=''):
    if len(log_message) > 0:
        logger.info(log_message, extra_tags={'raw_path': raw_path, 'filename': filename})
    full_path = os.path.join(raw_path, filename)
    # Short circuit
    if not image_utils.image_can_be_processed(fits_utils.get_primary_header(full_path), runtime_context.db_address):
        logger.error('Image cannot be processed. Check to make sure the instrument '
                     'is in the database and that the OBSTYPE is recognized by BANZAI',
                     extra_tags={'raw_path': raw_path, 'filename': filename})
        return
    try:
        run(full_path, runtime_context)
    except Exception:
        logger.error(logs.format_exception(), extra_tags={'filename': filename})


def parse_directory_args(runtime_context=None, raw_path=None, extra_console_arguments=None):
    if extra_console_arguments is None:
        extra_console_arguments = []

    if runtime_context is None:
        if raw_path is None:
            extra_console_arguments += [RAW_PATH_CONSOLE_ARGUMENT]

            runtime_context = parse_args(extra_console_arguments=extra_console_arguments)

        if raw_path is None:
            raw_path = runtime_context.raw_path
    return runtime_context, raw_path


def reduce_directory(runtime_context=None, raw_path=None, image_types=None):
    # TODO: Remove image_types once reduce_night is not needed
    runtime_context, raw_path = parse_directory_args(runtime_context, raw_path)
    process_directory(runtime_context, raw_path, image_types=image_types,
                      log_message='Reducing all frames in directory')


def reduce_single_frame(runtime_context=None):
    extra_console_arguments = [{'args': ['--filename'],
                                'kwargs': {'dest': 'filename', 'help': 'Name of file to process'}}]
    runtime_context, raw_path = parse_directory_args(runtime_context, extra_console_arguments=extra_console_arguments)
    process_single_frame(runtime_context, raw_path, runtime_context.filename)


def stack_calibrations(runtime_context=None, raw_path=None):
    extra_console_arguments = [{'args': ['--site'],
                                'kwargs': {'dest': 'site', 'help': 'Site code (e.g. ogg)', 'required': True}},
                               {'args': ['--enclosure'],
                                'kwargs': {'dest': 'enclosure', 'help': 'Enclosure code (e.g. clma)', 'required': True}},
                               {'args': ['--telescope'],
                                'kwargs': {'dest': 'telescope', 'help': 'Telescope code (e.g. 0m4a)', 'required': True}},
                               {'args': ['--camera'],
                                'kwargs': {'dest': 'camera', 'help': 'Camera (e.g. kb95)', 'required': True}},
                               {'args': ['--frame-type'],
                                'kwargs': {'dest': 'frame_type', 'help': 'Type of frames to process',
                                           'choices': ['bias', 'dark', 'skyflat'], 'required': True}},
                               {'args': ['--min-date'],
                                'kwargs': {'dest': 'min_date', 'required': True, 'type': date_utils.validate_date,
                                           'help': 'Earliest observation time of the individual calibration frames. '
                                                   'Must be in the format "YYYY-MM-DDThh:mm:ss".'}},
                               {'args': ['--max-date'],
                                'kwargs': {'dest': 'max_date', 'required': True, 'type': date_utils.validate_date,
                                           'help': 'Latest observation time of the individual calibration frames. '
                                                   'Must be in the format "YYYY-MM-DDThh:mm:ss".'}}]

    runtime_context, raw_path = parse_directory_args(runtime_context, raw_path,
                                                     extra_console_arguments=extra_console_arguments)
    instrument = dbs.query_for_instrument(runtime_context.db_address, runtime_context.site, runtime_context.camera,
                                          runtime_context.enclosure, runtime_context.telescope)
    calibrations.process_master_maker(runtime_context, instrument,  runtime_context.frame_type.upper(),
                                      runtime_context.min_date, runtime_context.max_date)


def e2e_stack_calibrations(runtime_context=None, raw_path=None):
    extra_console_arguments = [{'args': ['--site'],
                                'kwargs': {'dest': 'site', 'help': 'Site code (e.g. ogg)', 'required': True}},
                               {'args': ['--frame-type'],
                                'kwargs': {'dest': 'frame_type', 'help': 'Type of frames to process',
                                           'choices': ['bias', 'dark', 'skyflat'], 'required': True}},
                               {'args': ['--min-date'],
                                'kwargs': {'dest': 'min_date', 'required': True, 'type': date_utils.validate_date,
                                           'help': 'Earliest observation time of the individual calibration frames. '
                                                   'Must be in the format "YYYY-MM-DDThh:mm:ss".'}},
                               {'args': ['--max-date'],
                                'kwargs': {'dest': 'max_date', 'required': True, 'type': date_utils.validate_date,
                                           'help': 'Latest observation time of the individual calibration frames. '
                                                   'Must be in the format "YYYY-MM-DDThh:mm:ss".'}}]

    runtime_context, raw_path = parse_directory_args(runtime_context, raw_path,
                                                     extra_console_arguments=extra_console_arguments)
    schedule_stacking_checks(runtime_context)


def run_realtime_pipeline():
    extra_console_arguments = [{'args': ['--n-processes'],
                                'kwargs': {'dest': 'n_processes', 'default': 12,
                                           'help': 'Number of listener processes to spawn.', 'type': int}},
                               {'args': ['--broker-url'],
                                'kwargs': {'dest': 'broker_url', 'default': 'localhost',
                                           'help': 'URL for the broker service.'}},
                               {'args': ['--queue-name'],
                                'kwargs': {'dest': 'queue_name', 'default': 'banzai_pipeline',
                                           'help': 'Name of the queue to listen to from the fits exchange.'}}]

    runtime_context = parse_args(parser_description='Reduce LCO imaging data in real time.',
                                 extra_console_arguments=extra_console_arguments)

    # Need to keep the amqp logger level at least as high as INFO,
    # or else it send heartbeat check messages every second
    logging.getLogger('amqp').setLevel(max(logger.level, getattr(logging, 'INFO')))

    try:
        dbs.populate_instrument_tables(db_address=runtime_context.db_address)
    except Exception:
        logger.error('Could not connect to the configdb: {error}'.format(error=logs.format_exception()))

    logger.info('Starting pipeline listener')

    fits_exchange = Exchange('fits_files', type='fanout')
    listener = RealtimeModeListener(runtime_context)

    with Connection(runtime_context.broker_url) as connection:
        listener.connection = connection.clone()
        listener.queue = Queue(runtime_context.queue_name, fits_exchange)
        try:
            listener.run()
        except listener.connection.connection_errors:
            listener.connection = connection.clone()
            listener.ensure_connection(max_retries=10)
        except KeyboardInterrupt:
            logger.info('Shutting down pipeline listener.')


def mark_frame(mark_as):
    parser = argparse.ArgumentParser(description="Set the is_bad flag to mark the frame as {mark_as}"
                                                 "for a calibration frame in the database ".format(mark_as=mark_as))
    parser.add_argument('--filename', dest='filename', required=True,
                        help='Name of calibration file to be marked')
    parser.add_argument('--db-address', dest='db_address',
                        default='mysql://cmccully:password@localhost/test',
                        help='Database address: Should be in SQLAlchemy form')
    parser.add_argument("--log-level", default='debug', choices=['debug', 'info', 'warning',
                                                                 'critical', 'fatal', 'error'])

    args = parser.parse_args()
    logs.set_log_level(args.log_level)

    logger.info("Marking the frame {filename} as {mark_as}".format(filename=args.filename, mark_as=mark_as))
    dbs.mark_frame(args.filename, mark_as, db_address=args.db_address)
    logger.info("Finished")


def add_instrument():
    parser = argparse.ArgumentParser(description="Add a new instrument to the database")
    parser.add_argument("--site", help='Site code (e.g. ogg)', required=True)
    parser.add_argument('--enclosure', help= 'Enclosure code (e.g. clma)', required=True)
    parser.add_argument('--telescope', help='Telescope code (e.g. 0m4a)', required=True)
    parser.add_argument("--camera", help='Camera (e.g. kb95)', required=True)
    parser.add_argument("--camera-type", dest='camera_type',
                        help="Camera type (e.g. 1m0-SciCam-Sinistro)", required=True)
    parser.add_argument("--schedulable", help="Mark the instrument as schedulable", action='store_true',
                        dest='schedulable', default=False)
    parser.add_argument('--db-address', dest='db_address', default='sqlite:///test.db',
                        help='Database address: Should be in SQLAlchemy format')
    args = parser.parse_args()
    instrument = {'site': args.site,
                  'enclosure': args.enclosure,
                  'telescope': args.telescope,
                  'camera': args.camera,
                  'type': args.camera_type,
                  'schedulable': args.schedulable}
    with dbs.get_session(db_address=args.db_address) as db_session:
        dbs.add_instrument(instrument, db_session)


def mark_frame_as_good():
    mark_frame("good")


def mark_frame_as_bad():
    mark_frame("bad")


def update_db():
    parser = argparse.ArgumentParser(description="Query the configdb to ensure that the instruments table"
                                                 "has the most up-to-date information")

    parser.add_argument("--log-level", default='debug', choices=['debug', 'info', 'warning',
                                                                 'critical', 'fatal', 'error'])
    parser.add_argument('--db-address', dest='db_address',
                        default='mysql://cmccully:password@localhost/test',
                        help='Database address: Should be in SQLAlchemy form')
    args = parser.parse_args()
    logs.set_log_level(args.log_level)

    try:
        dbs.populate_instrument_tables(db_address=args.db_address)
    except Exception:
        logger.error('Could not populate instruments table: {error}'.format(error=logs.format_exception()))
