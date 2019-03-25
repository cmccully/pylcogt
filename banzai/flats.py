import os.path
import logging

from astropy.nddata.utils import block_reduce
import numpy as np

from banzai.utils import stats
from banzai.stages import Stage
from banzai.calibrations import CalibrationStacker, ApplyCalibration, CalibrationComparer

logger = logging.getLogger(__name__)


class FlatNormalizer(Stage):
    def __init__(self, pipeline_context):
        super(FlatNormalizer, self).__init__(pipeline_context)

    def do_stage(self, image):
        # Get the sigma clipped mean of the central 25% of the image
        flat_normalization = stats.sigma_clipped_mean(image.get_inner_image_section(), 3.5)
        image.data /= flat_normalization
        image.header['FLATLVL'] = flat_normalization
        logger.info('Calculate flat normalization', image=image,
                    extra_tags={'flat_normalization': flat_normalization})
        return image


class FlatMaker(CalibrationStacker):
    def __init__(self, pipeline_context):
        super(FlatMaker, self).__init__(pipeline_context)

    @property
    def calibration_type(self):
        # TODO: Do not hardcode flat type?
        return 'DOMEFLAT'

    def make_master_calibration_frame(self, images):
        master_image = super(FlatMaker, self).make_master_calibration_frame(images)
        master_image.bpm = np.logical_or(master_image.bpm, master_image.data < 0.2)
        master_image.data[master_image.bpm] = 1.0
        return master_image


class FlatDivider(ApplyCalibration):
    def __init__(self, pipeline_context):

        super(FlatDivider, self).__init__(pipeline_context)

    @property
    def calibration_type(self):
        return 'DOMEFLAT'

    def apply_master_calibration(self, image, master_calibration_image):

        master_flat_filename = master_calibration_image.filename
        master_flat_data = master_calibration_image.data
        master_flat_bpm = master_calibration_image.bpm
        if (image.data.shape != master_flat_data.shape)  & (image.confmode == "lco2_500kHz_binned_window"):
            # Ugly hack to force a resampled cutout from full frame readout into framed / binned readout
            master_flat_data = master_flat_data[1024:3072,1024:3072]
            rows,cols = master_flat_data.shape
            logger.info ("Windowing / resampling full frame flat field fro readout mode, input size: {} {}".format (rows,cols))
            master_flat_data = master_flat_data.reshape(rows//2, 2, cols//2, 2).sum(axis=(1,3))/4.
            master_flat_bpm = master_calibration_image.bpm[1023:3071,1023:3071].reshape(rows//2,2,cols//2,2).sum(axis=(1,3))
            logger.info ("Resampled flat field data to shape {}".format(master_flat_data.shape))

        logging_tags = {'master_flat': os.path.basename(master_calibration_image.filename)}
        logger.info('Flattening image', image=image, extra_tags=logging_tags)
        image.data /= master_flat_data
        image.bpm |= master_flat_bpm
        master_flat_filename = os.path.basename(master_flat_filename)
        image.header['L1IDFLAT'] = (master_flat_filename, 'ID of flat frame')
        image.header['L1STATFL'] = (1, 'Status flag for flat field correction')

        return image


class FlatComparer(CalibrationComparer):
    def __init__(self, pipeline_context):
        super(FlatComparer, self).__init__(pipeline_context)

    @property
    def calibration_type(self):
        return 'SKYFLAT'

    @property
    def reject_image(self):
        return False

    def noise_model(self, image):
        flat_normalization = float(image.header['FLATLVL'])
        poisson_noise = np.where(image.data > 0, image.data * flat_normalization, 0.0)
        noise = (image.readnoise ** 2.0 + poisson_noise) ** 0.5
        noise /= flat_normalization
        return noise
