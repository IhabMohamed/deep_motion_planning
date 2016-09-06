# -*- coding: utf-8 -*-

from __future__ import print_function

import os
import logging
import argparse

import pandas as pd

def parse_args():
    """Parse input arguments."""
    parser = argparse.ArgumentParser(description='Fuse csv files and prepare data')
    parser.add_argument('input_path', help='Folder of the raw data')
    parser.add_argument('--sort', help='Sort the list of .csv files by number of samples',
            action='store_true')

    def check_extension(extensions, filename):
        ext = os.path.splitext(filename)[1]
        if ext not in extensions:
            parser.error("Unsupported file extension: Use {}".format(extensions))
        return filename

    parser.add_argument('output_file', help='Filename of the processed data (stored in \
            ./data/processed)', type=lambda
            s:check_extension([".h5"],s))
    args = parser.parse_args()

    return args

def sorted_by_elements(files):
    num_elements = [-1] * len(files)
    for i,f in enumerate(files):
        print('({}/{}) {}'.format(i, len(files), f), end='\r')
        current = pd.read_csv(f)
        num_elements[i] = current.shape[0]

    data = [x for x in zip(files,num_elements)]
    data.sort(key=lambda x: x[1])

    return [filename for filename,_ in data]

def get_target_id(x):
    return int(os.path.basename(x).split('_')[-1].split('.')[0])

def main(project_dir):
    logger = logging.getLogger(__name__)
    logger.info('making final data set from raw data')

    args = parse_args()

    # Generate a list with all the csv files in the input folder
    path = os.path.join(project_dir, args.input_path)
    all_files = [os.path.join(path,f) for f in os.listdir(path) 
                         if os.path.isfile(os.path.join(path, f)) and f.split('.')[-1] == 'csv']

    # Sort by number of elements if requested
    if args.sort:
        logger.info('Sort list of files by number of elements')
        all_files = sorted_by_elements(all_files)
    else:
        logger.info('Sort list of files by target id')
        # Sort them by the integer number
        all_files.sort(key=lambda x: get_target_id(x))

    logger.info('Done: List of files is sorted')

    target_file = os.path.join(project_dir, 'data', 'processed', args.output_file)

    # Do not overwrite existing data containers
    if os.path.exists(target_file):
        logger.error('Target file already exists: {}'.format(target_file))
        exit()

    with pd.HDFStore(target_file) as store:
        num_elements = 0

        # Iterate over all input files and add them to the HDF5 container
        for i,f in enumerate(all_files):
            print('({}/{}) {}'.format(i, len(all_files), f), end='\r')
            current = pd.read_csv(f)

            # Make sure the final dataframe has a continous index
            current['original_index'] = current.index
            current['target_id'] = get_target_id(f)
            current.index = pd.Series(current.index) + num_elements
            store.append('data', current)

            num_elements += current.shape[0]
                
        print('')

    logger.info('We combined {} lines'.format(num_elements))
    logger.info('File saved: {}'.format(target_file))


if __name__ == '__main__':
    log_fmt = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    logging.basicConfig(level=logging.INFO, format=log_fmt)

    # not used in this stub but often useful for finding various files
    project_dir = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
    
    main(project_dir)

