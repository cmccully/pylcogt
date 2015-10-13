from __future__ import absolute_import, print_function, division

import os
from sqlalchemy.sql import func, expression
import itertools

from . import dbs
from .utils import date_utils
from . import logs

__author__ = 'cmccully'


class Stage(object):
    def __init__(self, stage_function, processed_path='', initial_query=None, group_by=None,
                 logger_name='', log_message='', cal_type=''):
        self.stage_function = stage_function
        self.processed_path = processed_path
        self.initial_query = initial_query
        self.group_by = group_by
        self.db_session = dbs.get_session()
        self.logger_name = logger_name
        self.log_message = log_message
        self.cal_type = cal_type

    def __del__(self):
        self.db_session.close()

    def select_input_images(self, telescope, epoch):
        # Select only the images we want to work on
        query = self.initial_query & (dbs.Image.telescope_id == telescope.id)
        query &= (dbs.Image.dayobs == epoch)

        if self.group_by is not None:
            config_list = []
            # Get the distinct values of ccdsum and filters
            for group_by in self.group_by:
                config_query = self.db_session.query(group_by)
                distinct_configs = config_query.filter(query).distinct().all()
                config_list.append([x[0] for x in distinct_configs])
            config_queries = []

            for config in itertools.product(*config_list):
                config_query = query

                for i in range(len(self.group_by)):
                    # Select images with the correct binning/filter
                    config_query &= (self.group_by[i] == config[i])
                config_queries.append(config_query)

        else:
            config_queries = [expression.true()]

        input_image_list = []
        config_list = []
        for image_config in config_queries:
            image_query = image_config & query

            image_list = self.db_session.query(dbs.Image).filter(image_query).all()

            # Convert from image objects to file names
            input_image_list.append(image_list)

            config_list.append(image_list[0])
        return input_image_list, config_list

    # By default don't return any output images
    def get_output_images(self, telescope, epoch):
        return None

    def make_output_directory(self, epoch, telescope):
            # Create output directory if necessary
            output_directory = os.path.join(self.processed_path, telescope.site,
                                            telescope.instrument, epoch)
            if not os.path.exists(output_directory):
                os.makedirs(output_directory)

    # By default we don't need to get a calibration image
    def get_calibration_image(self, epoch, telescope, image_config):
        return None

    def run(self, epoch_list, telescope_list):

        for epoch, telescope in itertools.product(epoch_list, telescope_list):
            self.make_output_directory(epoch, telescope)

            image_sets, image_configs = self.select_input_images(telescope, epoch)
            logger = logs.get_logger(self.logger_name)

            for images, image_config in zip(image_sets, image_configs):
                log_message = self.log_message.format(instrument=telescope.instrument, epoch=epoch,
                                                      site=telescope.site, binning=image_config.ccdsum,
                                                      filter_name=image_config.filter_name)
                logger.info(log_message)

                stage_args = [images]

                output_images = self.get_output_images(telescope, epoch)
                if output_images is not None:
                    stage_args.append(output_images)

                master_cal_file = self.get_calibration_image(epoch, telescope, image_config)
                if master_cal_file is not None:
                    stage_args.append(master_cal_file)

                self.stage_function(*stage_args)


class MakeCalibrationImage(Stage):
    def __init__(self, stage_function, processed_path='', initial_query=None, group_by=None,
                 logger_name='', log_message='', cal_type=''):

        query = initial_query & (dbs.Image.obstype == cal_type)
        super(MakeCalibrationImage, self).__init__(stage_function, processed_path=processed_path,
                                                   initial_query=query, group_by=group_by,
                                                   logger_name=logger_name, log_message=log_message, cal_type=cal_type)

    def get_calibration_image(self, epoch, telescope, image_config):
        output_directory = os.path.join(self.processed_path, telescope.site, telescope.instrument, epoch)
        cal_file = '{filepath}/{cal_type}_{instrument}_{epoch}_bin{bin}{filter}.fits'
        if dbs.Image.filter_name in self.group_by:
            filter_str = '_{filter}'.format(filter=image_config.filter_name)
        else:
            filter_str = ''

        cal_file = cal_file.format(filepath=output_directory, instrument=telescope.instrument, epoch=epoch,
                                   bin=image_config.ccdsum.replace(' ', 'x'), cal_type=self.cal_type, filter=filter_str)
        return cal_file

    def get_output_images(self, telescope, epoch):
        return None

    def save_calibration_info(self, cal_type, output_file, image_config):
        # Store the information into the calibration table
        # Check and see if the bias file is already in the database
        image_query = self.db_session.query(dbs.Calibration_Image)
        output_filename = os.path.basename(output_file)
        image_query = image_query.filter(dbs.Calibration_Image.filename == output_filename)
        image_query = image_query.all()

        if len(image_query) == 0:
            # Create a new row
            calibration_image = dbs.Calibration_Image()
        else:
            # Otherwise update the existing data
            # In principle we could just skip this, but this should be fast
            calibration_image = image_query[0]

        calibration_image.dayobs = image_config.dayobs
        calibration_image.ccdsum = image_config.ccdsum
        calibration_image.filter_name = image_config.filter_name
        calibration_image.telescope_id = image_config.telescope_id
        calibration_image.type = cal_type.upper()
        calibration_image.filename = output_filename
        calibration_image.filepath = os.path.dirname(output_file)

        self.db_session.add(calibration_image)
        self.db_session.commit()


class ApplyCalibration(Stage):
    def __init__(self, stage_function, processed_path='', initial_query=None, group_by=None,
                 logger_name='', log_message='', cal_type=''):
        super(ApplyCalibration, self).__init__(stage_function, processed_path=processed_path,
                                               initial_query=initial_query, group_by=group_by,
                                               logger_name=logger_name, log_message=log_message,
                                               cal_type=cal_type)

    def get_output_images(self, telescope, epoch):
        image_sets, image_configs = self.select_input_images(telescope, epoch)
        return [image for image_set in image_sets for image in image_set]


    def get_calibration_image(self, epoch, telescope, image_config):
        calibration_criteria = dbs.Calibration_Image.type == self.cal_type.upper()
        calibration_criteria &= dbs.Calibration_Image.telescope_id == telescope.id
        for criteria in self.group_by:
            group_by_field = vars(criteria)['key']
            calibration_criteria &= getattr(dbs.Calibration_Image, group_by_field) == getattr(image_config, group_by_field)

        calibration_query = self.db_session.query(dbs.Calibration_Image).filter(calibration_criteria)
        epoch_datetime = date_utils.epoch_string_to_date(epoch)

        find_closest = func.DATEDIFF(epoch_datetime, dbs.Calibration_Image.dayobs)
        find_closest = func.ABS(find_closest)

        calibration_query = calibration_query.order_by(find_closest.desc())
        calibration_image = calibration_query.one()
        calibration_file = os.path.join(calibration_image.filepath, calibration_image.filename)

        return calibration_file