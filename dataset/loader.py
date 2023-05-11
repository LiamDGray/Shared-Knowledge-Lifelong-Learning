""" This file is for loading the relevant dataset

    init needs the following:
        root_path: this is the root path to the Collection_dataset folder
        dataset: integer dataset/task id
        set_type: string 
            'train': training
            'validation': validation
            'test': test (FINAL, don't use until after all hyperparameter tuning)
        input_type: string
            'original': original image
            'features': feature vector (feature vector of image (with normalization) passing through backbone)
        pipeline: OPTIONAL a torchvision transform pipeline

    If this file is run directly, it will test for the existence of all images for each dataset,
    for each type of input.

    helpful instance methods:
        unique_class_id_list
            list of every class_id for the current dataset/set/limits combination
        class_id_counts
            dict of every class_id and the counts of the number of images/vectors

    helpful static methods:
        verify_all_original_images_are_in_database
            Checks that all images in original_images folder are cataloged in the database.
        verify_everything
            Goes through all datasets, and verifies that every original image, and feature vector exists.
        get_list_of_datasets
            Returns a list of all datasets in database
"""


import os
import sqlite3
import sys
from dataclasses import dataclass
from glob import glob
from typing import Any, Optional

import h5py
import torchvision.transforms as TF
from PIL import Image
from torch import Tensor
from torch.utils.data import Dataset
from torchvision.transforms import functional
from torchvision.transforms.functional import InterpolationMode
from tqdm import tqdm
import random
import math
import pickle

VALID_IMAGE_EXTENSIONS = [
    "bmp",
    "gif",
    "jfif",
    "jpeg",
    "jpg",
    "pgm",
    "png",
    "ppm",
    "tif",
    "webp",
]

IMAGES_ROOT_PATH = "/lab/tmpig15b/u/name-pending_collection/"
VECTORS_ROOT_PATH = "/lab/tmpig15b/u/name-pending_collection_vectors/"
DATABASE_PATH = "/lab/tmpig15b/u/name-pending_collection/0_collection/all_images.sqlite"

SET_TYPES = ["train", "validation", "test"]
INPUT_TYPES = ["original", "features"]
VECTOR_TYPES = ["resnet50", "xception"]

# given by Yuecheng
resnet50_tf_pipeline = TF.Compose(
    [
        TF.Resize((224, 224)),
        TF.ToTensor(),
        TF.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
    ]
)

# resize, crop, and normalize (matches pipeline for xception in timm)
xception_tf_pipeline = TF.Compose(
    [
        TF.Resize(
            size=333,
            interpolation=InterpolationMode.BICUBIC,
            max_size=None,
            antialias=None,
        ),
        TF.CenterCrop(size=(299, 299)),
        TF.ToTensor(),
        TF.Normalize(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5]),
    ]
)


@dataclass
class CollectionImage:
    # data required for every image
    relative_path: str
    class_id: int
    set_id: int  # named to prevent issues in sqlite calls
    problem: int

    # since subject_id is rare, default to not requiring it
    # subject_id: int = -1

    # data that is calculated later
    # file_size: int = -1
    file_hash: str = ""
    # image_hash: str = ""
    # image_width: int = -1
    # image_height: int = -1
    # image_mode: str = ""
    # image_format: str = ""

    # feature vector
    feature_vector: Tensor = Tensor([])


class CollectionDataset(Dataset):
    def __init__(
        self,
        dataset_name: str,
        set_type: str,
        input_type: str,
        vector_type: Optional[str] = "resnet50",
        pipeline: Optional[Any] = resnet50_tf_pipeline,
        label_dict = None,
        verify_all_sets_all_types: bool = False,
        full_dataset = False
    ) -> None:
        super().__init__()
        """init function for the dataset"""
        random.seed(27)

        self.initialized_correctly: bool = False

        # check inputs
        root_path = CollectionDataset.sanitize_and_check_root_path(IMAGES_ROOT_PATH)
        assert len(dataset_name) != ""
        assert set_type in SET_TYPES
        assert input_type in INPUT_TYPES

        if input_type == "features":
            assert vector_type in VECTOR_TYPES

        # store inputs
        self.root_path = root_path
        self.dataset_name = dataset_name
        self.set_type = set_type
        self.input_type = input_type
        self.vector_type = vector_type
        self.pipeline = pipeline

        # get the dataset_name
        # query = f"select dataset_name FROM datasets where dataset={dataset};"
        # result = CollectionDataset.execute_database_query(root_path, query)
        # if len(result) == 0:
        #     print("no dataset with that id")
        #     return None

        # self.dataset_name = result[0][0]

        # grab the correct rows
        print("load relevant results from database...")
        set_type_int = SET_TYPES.index(self.set_type)
        if not verify_all_sets_all_types:
            query = f'SELECT file_hash, relative_path, class_id, set_id, problem FROM images WHERE dataset="{dataset_name}" AND problem=0 AND set_id={set_type_int};'
        else:
            query = f'SELECT file_hash, relative_path, class_id, set_id, problem FROM images WHERE dataset="{dataset_name}" AND problem=0;'
        rows = CollectionDataset.execute_database_query(query)

        if len(rows) == 0:
            print(
                f"no results for dataset_name={dataset_name} and set_type={set_type_int}"
            )
            return None

        self.images: list[CollectionImage] = []
        self.dict = {}
        self.label_dict = {}

        if full_dataset:
            self.sample_class_num = math.inf
            self.sample_train_size = math.inf
            self.sample_test_size = math.inf
        else:
            self.sample_class_num = 500
            self.sample_train_size = 54000
            self.sample_test_size = 6000
        for row in rows:
            if int(row[2]) not in self.dict:
                self.dict[int(row[2])] = [(row[0], row[1], row[3], row[4])]
            else:
                self.dict[int(row[2])].append((row[0], row[1], row[3], row[4]))
        
        if label_dict:
            self.label_dict = label_dict
        else:
            """
            modification here: first tries to implement an identity mapping if possible
            """
            if set(self.dict.keys()) == set(list(range(len(self.dict.keys())))):
                self.label_dict = {i:i for i in range(len(self.dict.keys()))}
            else:
                label_use = 0
                for label in self.dict.keys():
                    self.label_dict[label] = label_use
                    label_use += 1

        if len(self.dict.keys()) > self.sample_class_num:
            self.num_classes = self.sample_class_num
        else:
            self.num_classes = len(self.dict.keys())
            
        if self.set_type=="train" and len(rows) > self.sample_train_size:
            self.labels, self.hashes, self.paths, self.id_set, self.problem_list = self._random_value(self.dict, self.sample_train_size)
        elif (self.set_type == "validation" or self.set_type == "test") and len(rows) > self.sample_test_size:
            self.labels, self.hashes, self.paths, self.id_set, self.problem_list = self._random_value(self.dict, self.sample_test_size)
        else:
            self.labels, self.hashes, self.paths, self.id_set, self.problem_list = self._random_value(self.dict, math.inf)
        for i in range(len(self.labels)):
            self.labels[i] = self.label_dict[self.labels[i]]
            self.images.append(CollectionImage(file_hash=self.hashes[i], 
                                                relative_path=self.paths[i], 
                                                class_id=self.labels[i], 
                                                set_id=int(self.id_set[i]),
                                                problem=int(self.problem_list[i]),
                                                ))

        # store the details for the images
        # for row in rows:
        #     self.images += [
        #         CollectionImage(
        #             file_hash=row[0],
        #             relative_path=row[1],
        #             class_id=int(row[2]),
        #             set_id=int(row[3]),
        #             problem=int(row[4]),
        #         )
        #     ]

        if not verify_all_sets_all_types:
            if self.input_type == "original":
                self.initialized_correctly = self.verify_original_images_exist()
            else:  # features input
                self.initialized_correctly = (
                    self.verify_feature_vectors_exist_and_load()
                )
        else:
            # assume true, but set to false if any fail
            self.initialized_correctly = True
            if not self.verify_original_images_exist():
                self.initialized_correctly = False
            if not self.verify_feature_vectors_exist_and_load(skip_load=True):
                self.initialized_correctly = False

        if self.initialized_correctly:
            print("done")
        else:
            print("initialization failed")

    def _random_value(self, dictionary, amount):
        label = []
        hash = []
        path = []
        set_id = []
        problem_id = []
        if amount == math.inf:
            num_each_class = math.inf
        else:
            num_each_class = round(amount/self.num_classes)

        current_class_size = 0
        for key, values in dictionary.items():
            track_num = 0
            if key not in list(self.label_dict.keys())[:self.num_classes]:
                assert self.num_classes == self.sample_class_num
                if self.num_classes == self.sample_class_num:
                    continue
            random.shuffle(values)
            for value in values:
                label.append(key)
                hash.append(value[0])
                path.append(value[1])
                set_id.append(value[2])
                problem_id.append(value[3])
                track_num += 1
                if track_num >= num_each_class:
                    break
            current_class_size += 1
            if current_class_size >= self.num_classes:
                break
        return label, hash, path, set_id, problem_id


    def __len__(self) -> int:
        assert self.initialized_correctly

        return len(self.images)

    def __getitem__(self, index: int) -> tuple:
        assert self.initialized_correctly
        assert 0 <= index <= len(self.images)

        if self.input_type == "features":
            return self.images[index].feature_vector, self.images[index].class_id
        else:

            image_filename = self.original_image_path_for_index(index)

            with Image.open(image_filename) as image:

                # for the original images, make sure to convert to RGB
                if self.input_type == "original":
                    image = image.convert("RGB")

                # convert to a tensor before doing anything else
                # image_tensor = functional.to_tensor(image)

            # if there is a pipeline, run it
            if self.pipeline:
                image_tensor = self.pipeline(image)

            return image_tensor, self.images[index].class_id

    def original_image_path_for_index(self, index: int) -> str:
        """get the path to the original image"""
        assert 0 <= index <= len(self.images)

        return (
            self.root_path + self.dataset_name + "/" + self.images[index].relative_path
        )

    def verify_original_images_exist(self) -> bool:
        """check that all original image files exist"""

        had_error = False
        print("verifying that original images exist...")

        # order is not important
        for index in tqdm(range(len(self.images))):
            original_filename = self.original_image_path_for_index(index)
            if not os.path.isfile(original_filename):
                print("missing", original_filename)
                had_error = True

        return not had_error

    def verify_feature_vectors_exist_and_load(self, skip_load: bool = False) -> bool:
        """check that the hdf5 file exists, and load all of the feature vectors for the set"""

        if self.vector_type is None:
            print("error with vector_type")
            return False

        had_error = True
        feature_vectors_filename = (
            VECTORS_ROOT_PATH + self.vector_type + "/" + self.dataset_name + ".h5"
        )

        if not skip_load:
            print(
                f"verifying all {self.vector_type} feature vectors exist, and loading..."
            )
        else:
            print(f"verifying all {self.vector_type} feature vectors exist...")

        if not os.path.isfile(feature_vectors_filename):
            print("feature vector file is missing", feature_vectors_filename)
        else:

            all_hashes = []
            # force order to be the same
            for i in range(len(self.images)):
                all_hashes += [self.images[i].file_hash]

            with h5py.File(feature_vectors_filename) as file_h:
                # check that all hashes exist in file
                feature_vectors_hashes = list(file_h.keys())
                missing_list = list(set(all_hashes) - set(feature_vectors_hashes))

                if len(missing_list) > 0:
                    print("missing these feature vectors:", missing_list)
                else:
                    # if no issues, create matrix of feature vectors
                    if not skip_load:
                        for i, _ in enumerate(self.images):
                            self.images[i].feature_vector = file_h[self.images[i].file_hash][:]  # type: ignore
                    had_error = False

        return not had_error

    def get_entry_for_index(self, index: int) -> CollectionImage:
        """get the entry for a given index"""
        assert self.initialized_correctly
        assert 0 <= index <= len(self.images)

        return self.images[index]

    @property
    def unique_class_id_list(self) -> list:
        """get a list of unique class_id"""
        assert len(self.images) > 0

        all_class_ids = [x.class_id for x in self.images]
        # return an ordered and unique list
        return sorted(list(set(all_class_ids)))

    @property
    def class_id_counts(self) -> dict:
        """get the counts for all class_ids"""
        assert len(self.images) > 0

        counts = {}
        all_class_ids = [x.class_id for x in self.images]
        for class_id in self.unique_class_id_list:
            counts.update({class_id: all_class_ids.count(class_id)})

        return counts

    @staticmethod
    def test_if_valid_image(full_file_path: str) -> bool:
        """function to test if an image file is valid
        (this will catch most, but not 100% of all image issues)"""
        try:
            with Image.open(full_file_path) as image_h:
                try:
                    image_h.verify()
                    return True
                except:
                    # failed after the file opened, while trying to verify
                    return False
        except:
            # failed in trying to open the image file
            return False

    @staticmethod
    def sanitize_and_check_root_path(root_path: str) -> str:
        """make sure root path ends in '/', and that it exists"""
        root_path = (root_path + "/").replace("//", "/")
        assert os.path.isdir(root_path)

        return root_path

    @staticmethod
    def execute_database_query(query: str) -> list:
        """execute a query on the database at the path"""

        # root_path = CollectionDataset.sanitize_and_check_root_path(root_path)
        database_filename = DATABASE_PATH  # root_path + "database.sqlite"

        assert os.path.isfile(database_filename)

        with sqlite3.connect(database_filename) as sql_conn:
            cursor = sql_conn.cursor()
            cursor.execute(query)
            rows = cursor.fetchall()

        return rows

    @staticmethod
    def verify_everything(root_path: str) -> bool:
        """verify the existence of all problem-free files"""
        print("verify all sets all input types")

        root_path = CollectionDataset.sanitize_and_check_root_path(root_path)

        # get all of the datasets that are in files
        query = "select distinct dataset FROM images order by dataset;"
        rows = CollectionDataset.execute_database_query(query)
        dataset_list = [id[0] for id in rows]

        broken_dataset_list = []

        # loop through each dataset
        for dataset in dataset_list:
            print(f"#### dataset: {dataset} ####")
            Collection_dataset = CollectionDataset(
                root_path,
                dataset,
                "train",
                "original",
                verify_all_sets_all_types=True,
            )

            if not Collection_dataset.initialized_correctly:
                broken_dataset_list += [dataset]

        if len(broken_dataset_list) == 0:
            print("")
            print("########################")
            print("####    ALL GOOD    ####")
            print("########################")
            return True
        else:
            print("")
            print("######################################")
            print("####    SOME FILES ARE MISSING    ####")
            print("######################################")
            print("datasets with issues:", broken_dataset_list)
            return False

    @staticmethod
    def get_list_of_datasets(get_counts: bool = False) -> dict:
        """get list of all datasets in database
        it returns a dict with the key as the dataset, and the value
        is another dict with the name of the dataset and the good flag"""
        # root_path = CollectionDataset.sanitize_and_check_root_path(root_path)

        # get all of the datasets that are in images
        query = "select distinct dataset FROM images order by dataset;"
        rows = CollectionDataset.execute_database_query(query)

        Collection_datasets_dict = {}
        for row in rows:
            dataset_name = row[0]

            if not get_counts:
                Collection_datasets_dict.update(
                    {
                        dataset_name: {
                            "name": dataset_name,
                        }
                    }
                )
            else:
                # query all of the counts at the same time
                query = f'select count(*) FROM images WHERE problem=0 and dataset="{dataset_name}" group by set_id;'
                count_rows = CollectionDataset.execute_database_query(query)
                if len(count_rows) == 3:
                    set_counts = []
                    for set_i in range(3):
                        set_counts += [count_rows[set_i][0]]
                else:
                    set_counts = [0, 0, 0]
                total_count = sum(set_counts)

                Collection_datasets_dict.update(
                    {
                        dataset_name: {
                            "name": dataset_name,
                            "total_count": total_count,
                            "train_count": set_counts[0],
                            "validation_count": set_counts[1],
                            "test_count": set_counts[2],
                        }
                    }
                )

        return Collection_datasets_dict

    @staticmethod
    def verify_all_original_images_are_in_database(
        root_path: str,
    ) -> tuple:
        """check that every image in original_images is cataloged in the database"""

        root_path = CollectionDataset.sanitize_and_check_root_path(root_path)

        print("begin cataloging all files in original_images...")
        print("this will take several minutes")
        source_dir = root_path + "original_images/"
        all_files = glob("**/*.*", root_dir=source_dir, recursive=True)

        # loop through all files, and keep only those that are images (extension match)
        image_files = []
        for file in all_files:

            # get just the filename (this will also catch folders)
            after_final_slash = file.split("/")[-1]

            # first, check if possibly an image file (has an extension)
            # ignore all folders and files without an '.' in the name
            if after_final_slash.find(".") > -1:

                # then, get the extension
                extension = after_final_slash.split(".")[-1].lower()

                # check if extension is one of the valid image extension
                # ignore all other file types
                if extension in VALID_IMAGE_EXTENSIONS:
                    image_files += [file]

        print("done")
        print("images found in original_images folder:", len(image_files))

        # get all of the files in the database
        query = "select dataset || relative_path FROM images;"
        rows = CollectionDataset.execute_database_query(query)
        db_images = [row[0] for row in rows]
        print("images in database:", len(db_images))

        images_not_in_db = list(set(image_files) - set(db_images))
        db_images_without_files = list(set(db_images) - set(image_files))
        if (len(images_not_in_db) == 0) and (len(db_images_without_files) == 0):
            print(
                "all original images exist in database, and database contains no extras"
            )
            return True, images_not_in_db, db_images_without_files

        print("count of original images not in database", len(images_not_in_db))
        print("count of extra images in database", len(db_images_without_files))

        return False, images_not_in_db, db_images_without_files


if __name__ == "__main__":

    import pandas as pd
    df = pd.read_csv(f'dataset_key.csv')
    task_name_list = list(df["new_dataset_name"])
    # with open("full_dataset_stat.csv", "w") as f:
    #     f.write("task_id,task_name,num_classes,train_size,val_size,test_size\n")
    #     for i, task_name in enumerate(task_name_list):
    #         print(f"-------------------------start loading {task_name}: {i}/102-------------------------")
    #         train_dataset = CollectionDataset(task_name, 'train', 'features', vector_type='xception', pipeline=xception_tf_pipeline)
    #         val_dataset = CollectionDataset(task_name, 'validation', 'features', label_dict=train_dataset.label_dict, vector_type='xception', pipeline=xception_tf_pipeline)
    #         test_dataset = CollectionDataset(task_name, 'test', 'features', label_dict=train_dataset.label_dict, vector_type='xception', pipeline=xception_tf_pipeline)
    #         os.makedirs(f"dataset_detail/{task_name}", exist_ok=True)
    #         with open(f"dataset_detail/{task_name}/label_dict.pickle", 'wb') as handle:
    #             pickle.dump(train_dataset.label_dict, handle, protocol=pickle.HIGHEST_PROTOCOL)
    #         with open(f"dataset_detail/{task_name}/train.csv", "w") as sf:
    #             sf.write("image_path,image_label,image_hash\n")
    #             for specific_image in train_dataset.images:
    #                 sf.write(f"{specific_image.relative_path},{specific_image.class_id},{specific_image.file_hash}\n")
    #         with open(f"dataset_detail/{task_name}/val.csv", "w") as sf:
    #             sf.write("image_path,image_label,image_hash\n")
    #             for specific_image in val_dataset.images:
    #                 sf.write(f"{specific_image.relative_path},{specific_image.class_id},{specific_image.file_hash}\n")
    #         with open(f"dataset_detail/{task_name}/test.csv", "w") as sf:
    #             sf.write("image_path,image_label,image_hash\n")
    #             for specific_image in test_dataset.images:
    #                 sf.write(f"{specific_image.relative_path},{specific_image.class_id},{specific_image.file_hash}\n")

    #         f.write(f"{i},{task_name},{len(train_dataset.label_dict.keys())},{len(train_dataset)},{len(val_dataset)},{len(test_dataset)}\n")

    with open("full_dataset_stat.csv", "a") as f:
        # f.write("task_id,task_name,num_classes,train_size,val_size,test_size\n")
        for i, task_name in enumerate(task_name_list):
            if i < 102:
                continue
            print(f"-------------------------start loading {task_name}: {i+1}/107-------------------------")
            train_dataset = CollectionDataset(task_name, 'train', 'features', vector_type='xception', pipeline=xception_tf_pipeline)
            val_dataset = CollectionDataset(task_name, 'validation', 'features', label_dict=train_dataset.label_dict, vector_type='xception', pipeline=xception_tf_pipeline)
            test_dataset = CollectionDataset(task_name, 'test', 'features', label_dict=train_dataset.label_dict, vector_type='xception', pipeline=xception_tf_pipeline)
            os.makedirs(f"dataset_detail/{task_name}", exist_ok=True)
            with open(f"dataset_detail/{task_name}/label_dict.pickle", 'wb') as handle:
                pickle.dump(train_dataset.label_dict, handle, protocol=pickle.HIGHEST_PROTOCOL)
            with open(f"dataset_detail/{task_name}/train.csv", "w") as sf:
                sf.write("image_path,image_label,image_hash\n")
                for specific_image in train_dataset.images:
                    sf.write(f"{specific_image.relative_path},{specific_image.class_id},{specific_image.file_hash}\n")
            with open(f"dataset_detail/{task_name}/val.csv", "w") as sf:
                sf.write("image_path,image_label,image_hash\n")
                for specific_image in val_dataset.images:
                    sf.write(f"{specific_image.relative_path},{specific_image.class_id},{specific_image.file_hash}\n")
            with open(f"dataset_detail/{task_name}/test.csv", "w") as sf:
                sf.write("image_path,image_label,image_hash\n")
                for specific_image in test_dataset.images:
                    sf.write(f"{specific_image.relative_path},{specific_image.class_id},{specific_image.file_hash}\n")

            f.write(f"{i},{task_name},{len(train_dataset.label_dict.keys())},{len(train_dataset)},{len(val_dataset)},{len(test_dataset)}\n")