# Copyright 2022 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""This creates training and validation datasets for the model.

As inputs it takes a Sentinel-2 image consisting of 13 bands.
Each band contains data for a specific range of the electromagnetic spectrum.
    https://developers.google.com/earth-engine/datasets/catalog/COPERNICUS_S2_HARMONIZED

As outputs it returns the probabilities of each classification for every pixel.
The land cover labels for the training dataset come from the ESA WorldCover.
    https://developers.google.com/earth-engine/datasets/catalog/ESA_WorldCover_v100
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import List, Optional

import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions
import ee
import numpy as np
import requests

from serving import data

# Default values.
POINTS_PER_CLASS = 100
PATCH_SIZE = 128
MAX_REQUESTS = 20  # default EE request quota

# Simplified polygons covering most land areas in the world.
WORLD_POLYGONS = [
    # Americas
    [(-33.0, -7.0), (-55.0, 53.0), (-166.0, 65.0), (-68.0, -56.0)],
    # Africa, Asia, Europe
    [
        (74.0, 71.0),
        (166.0, 55.0),
        (115.0, -11.0),
        (74.0, -4.0),
        (20.0, -38.0),
        (-29.0, 25.0),
    ],
    # Australia
    [(170.0, -47.0), (179.0, -37.0), (167.0, -12.0), (128.0, 17.0), (106.0, -29.0)],
]


def sample_points(
    seed: int,
    polygons: list[list[tuple[float, float]]],
    points_per_class: int,
    scale: int = 1000,
) -> Iterable[tuple[float, float]]:
    """Selects around the same number of points for every classification.

    This expects the input image to be an integer, for balanced regression points
    you could do `image.int()` to truncate the values into an integer.
    If the values are too large, it might be good to bucketize, for example
    the range is between 0 and ~1000 `image.divide(100).int()` would give ~10 buckets.

    Args:
        seed: Random seed to make sure to get different results on different workers.
        polygons: List of polygons to pick points from.
        points_per_class: Number of points per classification to pick.
        scale: Number of meters per pixel, a smaller scale will take longer.

    Returns: An iterable of the picked points.
    """
    data.ee_init()
    image = data.get_label_image()
    region = ee.Geometry.MultiPolygon(polygons)
    points = image.stratifiedSample(
        points_per_class,
        region=region,
        scale=scale,
        seed=seed,
        geometries=True,
    )
    for point in points.toList(points.size()).getInfo():
        yield point["geometry"]["coordinates"]


def get_training_example(
    lonlat: tuple[float, float], patch_size: int = PATCH_SIZE
) -> tuple[np.ndarray, np.ndarray]:
    """Gets an (inputs, labels) training example for year 2020.

    Args:
        lonlat: A (longitude, latitude) pair for the point of interest.
        patch_size: Size in pixels of the surrounding square patch.

    Returns: An (inputs, labels) pair of NumPy arrays.
    """
    data.ee_init()
    return (
        data.get_input_patch(2020, lonlat, patch_size),
        data.get_label_patch(lonlat, patch_size),
    )


def try_get_example(
    lonlat: tuple[float, float], patch_size: int = PATCH_SIZE
) -> Iterable[tuple[np.ndarray, np.ndarray]]:
    """Wrapper over `get_training_examples` that allows it to simply log errors instead of crashing."""
    try:
        yield get_training_example(lonlat, patch_size)
    except requests.exceptions.HTTPError as e:
        logging.exception(e)


def serialize_tensorflow(inputs: np.ndarray, labels: np.ndarray) -> bytes:
    """Serializes inputs and labels NumPy arrays as a tf.Example.

    Both inputs and outputs are expected to be dense tensors, not dictionaries.
    We serialize both the inputs and labels to save their shapes.

    Args:
        inputs: The example inputs as dense tensors.
        labels: The example labels as dense tensors.

    Returns: The serialized tf.Example as bytes.
    """
    import tensorflow as tf

    features = {
        name: tf.train.Feature(
            bytes_list=tf.train.BytesList(value=[tf.io.serialize_tensor(data).numpy()])
        )
        for name, data in {"inputs": inputs, "labels": labels}.items()
    }
    example = tf.train.Example(features=tf.train.Features(feature=features))
    return example.SerializeToString()


def run_tensorflow(
    data_path: str,
    points_per_class: int = POINTS_PER_CLASS,
    patch_size: int = PATCH_SIZE,
    max_requests: int = MAX_REQUESTS,
    polygons: list[list[tuple[float, float]]] = WORLD_POLYGONS,
    beam_args: Optional[List[str]] = None,
) -> None:
    """Runs an Apache Beam pipeline to create a dataset.

    This fetches data from Earth Engine and creates a TFRecords dataset.
    We use `max_requests` to limit the number of concurrent requests to Earth Engine
    to avoid quota issues. You can request for an increas of quota if you need it.

    Args:
        data_path: Directory path to save the TFRecord files.
        points_per_class: Number of points to get for each classification.
        patch_size: Size in pixels of the surrounding square patch.
        max_requests: Limit the number of concurrent requests to Earth Engine.
        polygons: List of polygons to pick points from.
        beam_args: Apache Beam command line arguments to parse as pipeline options.
    """
    # Equally divide the number of points by the number of concurrent requests.
    num_points = max(int(points_per_class / max_requests), 1)

    beam_options = PipelineOptions(
        beam_args,
        save_main_session=True,
        setup_file="./setup.py",
        max_num_workers=max_requests,  # distributed runners
        direct_num_workers=max(max_requests, 20),  # direct runner
        disk_size_gb=50,
    )
    with beam.Pipeline(options=beam_options) as pipeline:
        (
            pipeline
            | "🌱 Make seeds" >> beam.Create(range(max_requests))
            | "📌 Sample points" >> beam.FlatMap(sample_points, polygons, num_points)
            | "🃏 Reshuffle" >> beam.Reshuffle()
            | "📑 Get examples" >> beam.FlatMap(try_get_example, patch_size)
            | "✍🏽 Serialize" >> beam.MapTuple(serialize_tensorflow)
            | "📚 Write TFRecords"
            >> beam.io.WriteToTFRecord(
                f"{data_path}/part", file_name_suffix=".tfrecord.gz"
            )
        )


if __name__ == "__main__":
    import argparse
    import logging

    logging.getLogger().setLevel(logging.INFO)

    parser = argparse.ArgumentParser()
    parser.add_argument("framework", choices=["tensorflow"])
    parser.add_argument(
        "--data-path",
        required=True,
        help="Directory path to save the TFRecord files.",
    )
    parser.add_argument(
        "--points-per-class",
        default=POINTS_PER_CLASS,
        type=int,
        help="Number of points to get for each classification.",
    )
    parser.add_argument(
        "--patch-size",
        default=PATCH_SIZE,
        type=int,
        help="Size in pixels of the surrounding square patch.",
    )
    parser.add_argument(
        "--max-requests",
        default=MAX_REQUESTS,
        type=int,
        help="Limit the number of concurrent requests to Earth Engine.",
    )
    args, beam_args = parser.parse_known_args()

    if args.framework == "tensorflow":
        run_tensorflow(
            data_path=args.data_path,
            points_per_class=args.points_per_class,
            patch_size=args.patch_size,
            max_requests=args.max_requests,
            beam_args=beam_args,
        )
    else:
        raise ValueError(f"framework not supported: {args.framework}")



#-----------------CLOUD-----------------------
def get_s2(aoi, start_date, end_date):
    # Import and filter S2
    
    CLOUD_FILTER = 20
    CLD_PRB_THRESH = 5
    NIR_DRK_THRESH = 0.15
    CLD_PRJ_DIST = 1
    BUFFER = 1

    s2_sr_col = (ee.ImageCollection('COPERNICUS/S2_SR')
                 .filterBounds(aoi)
                 .filterDate(start_date, end_date)
                 .filter(ee.Filter.lte('CLOUDY_PIXEL_PERCENTAGE', CLOUD_FILTER))
                 .map(lambda im: im.clip(aoi)))

    # Import and filter s2cloudless.
    s2_cloudless_col = (ee.ImageCollection('COPERNICUS/S2_CLOUD_PROBABILITY')
                        .filterBounds(aoi)
                        .filterDate(start_date, end_date))

    # Join the filtered s2cloudless collection to the SR collection by the 'system:index' property.
    return ee.ImageCollection(ee.Join.saveFirst('s2cloudless').apply(primary = s2_sr_col, 
                                                                      secondary = s2_cloudless_col, 
                                                                      condition = ee.Filter.equals(leftField='system:index', rightField='system:index')))

# Cloud probability threshold.
CLD_PRB_THRESH = 50
NIR_DRK_THRESH = 0.15
CLD_PRJ_DIST = 2
BUFFER = 8

def cloud_mask(img):
    
    # Add cloud component bands.
    def add_cloud_bands(img):
        # Get s2cloudless image, subset the probability band.
        cld_prb = ee.Image(img.get('s2cloudless')).select('probability')
        # Condition s2cloudless by the probability threshold value.
        is_cloud = cld_prb.gt(CLD_PRB_THRESH).rename('clouds')
        # Add the cloud probability layer and cloud mask as image bands.
        return img.addBands(ee.Image([cld_prb, is_cloud]))
    
    img_cloud = add_cloud_bands(img)
   
    # Add cloud shadow component bands.
    def add_shadow_bands(img):
        # Identify water pixels from the SCL band.
        not_water = img.select('SCL').neq(6)
        # Identify dark NIR pixels that are not water (potential cloud shadow pixels).
        SR_BAND_SCALE = 1e4
        dark_pixels = img.select('B8').lt(NIR_DRK_THRESH*SR_BAND_SCALE).multiply(not_water).rename('dark_pixels')
        # Determine the direction to project cloud shadow from clouds (assumes UTM projection).
        shadow_azimuth = ee.Number(90).subtract(ee.Number(img.get('MEAN_SOLAR_AZIMUTH_ANGLE')))
        # Project shadows from clouds for the distance specified by the CLD_PRJ_DIST input.
        cld_proj = (img.select('clouds').directionalDistanceTransform(shadow_azimuth, CLD_PRJ_DIST*10)
            .reproject(crs=img.select(0).projection().crs(), scale=100)
            .select('distance')
            .mask()
            .rename('cloud_transform'))
        # Identify the intersection of dark pixels with cloud shadow projection.
        shadows = cld_proj.multiply(dark_pixels).rename('shadows')
        # Add dark pixels, cloud projection, and identified shadows as image bands.
        return img.addBands(ee.Image([dark_pixels, cld_proj, shadows]))
    
    img_cloud_shadow = add_shadow_bands(img_cloud)

    # Combine cloud and shadow mask, set cloud and shadow as value 1, else 0.
    is_cld_shdw = img_cloud_shadow.select('clouds').add(img_cloud_shadow.select('shadows')).gt(0)

    # Remove small cloud-shadow patches and dilate remaining pixels by BUFFER input.
    # 20 m scale is for speed, and assumes clouds don't require 10 m precision.
    is_cld_shdw2 = (is_cld_shdw.focal_min(2).focal_max(BUFFER*2/20)
        .reproject(crs=img.select([0]).projection().crs(), scale=20)
        .rename('cloudmask'))

    # Add the final cloud-shadow mask to the image.
    return img_cloud_shadow.addBands(is_cld_shdw2)
    
    
# function to mosaic all s2 data
def mosaicByDate1(imgCol):
  imlist = imgCol.toList(imgCol.size())
  unique_dates = imlist.map(lambda im: ee.Image(im).date().format("YYYY-MM-dd")).distinct()
  
  def mosaic_image(d):
    d = ee.Date(d)
    im = imgCol.filterDate(d, d.advance(1, "day")).mosaic()
    return im.set("system:time_start", d.millis(), "system:id", d.format("YYYY-MM-dd"))
  
  mosaic_imlist = unique_dates.map(mosaic_image)
  return ee.ImageCollection(mosaic_imlist)
  
  
  
from datetime import datetime
def label_mask(start_date, end_date, roi):
  date_string = start_date # replace with your date string
  date_obj = datetime.strptime(date_string, "%Y-%m-%d")
  year = ee.String(str(date_obj.year))
  crop = ee.ImageCollection('AAFC/ACI').filterDate(start_date, end_date)
  mask = crop.map(lambda image: image.expression("b('landcover') == 153 || b('landcover') == 133 || b('landcover') == 146 "))
  mask = ee.Image(mask.first())
  maskedImage = crop.map(lambda image: image.mask(mask).unmask().clip(roi).rename('landcover_'+year.getInfo()))
  return maskedImage
  
def ndvi_collection(imageCollection, start_month, end_month):
    # Define the months to create composites for
    months = ee.List.sequence(start_month, end_month)

    # Create composites for each month using median reducer
    composites = ee.ImageCollection.fromImages(
        months.map(lambda m: imageCollection.filter(ee.Filter.calendarRange(m, m, 'month'))
                                     .median()
                                     .addBands(imageCollection.filter(ee.Filter.calendarRange(m, m, 'month'))
                                     .median().normalizedDifference(['B8', 'B4']).multiply(1000).rename('ndvi').set('month', m))
                  )
    )

    return composites.select('B2','B3','B4','ndvi')