# from gnutools.fs import load_config as _load_config, parent
from gnutools.fs import parent
import os
from tqdm import tqdm
import re
# from gnutools.utils import id_generator
from gnutools import fs

from zakuro.var import __FILESTORE__
from zakuro.var import __ZFS_URI__
def PathURI(src):
    if not src.startswith(f"{__ZFS_URI__}://"):
        if src.startswith(f"/{__ZFS_URI__}/"):
            src = f"{__ZFS_URI__}://{src[len('/zfs/'):]}"
        elif src.startswith(f"{__ZFS_URI__}/"):
            splits = src.split(f"{__ZFS_URI__}/")[1].split("/")
            partition, group = splits[0].split("-")
            partition = partition.lower()
            group = group.upper()
            prefix = "/".join(splits[1:])
            src = f"{__ZFS_URI__}://{group}/{partition}/{prefix}"
        else:
            raise Exception
    return src


def Path(src):
    src = PathURI(src)
    return f"/{__ZFS_URI__}/{src.split('zfs://')[1]}"


class Object:
    def __init__(
        self,
        filepath,
        host=None,
        username=None,
        password=None,
        read_binary=False,
        secure=False,
        version_id=0,
    ):
        if host is not None:
            from minio import Minio

            self._client = Minio(
                host,
                secure=secure,
                access_key=username,
                secret_key=password,
            )
        else:
            from miniofs import client

            self._client = client
        self._filepath = filepath
        self._version_id = version_id
        if read_binary:
            self._cdata = self.collect()
        else:
            self._cdata = None

    # def collect(self):
    #     if self._filepath.startswith("/zfs/"):
    #         filepath = "zfs://" + self._filepath[5:]
    #     else:
    #         assert self._filepath.startswith("zfs://")
    #         filepath = self._filepath
    #     output_file = f"/tmp/.miniofs_{id_generator(128)}"
    #     bucket, object_name = split_bucket_name(filepath)
    #     self._client.fget_object(
    #         bucket, object_name, output_file, version_id=self._version_id
    #     )
    #     binary = open(output_file, "rb").read()
    #     os.remove(output_file)
    #     self._cdata = binary
    #     return self._cdata





def allow_spark(spark):
    from zakuro.functional import load_config
    cfg = load_config()
    spark.sparkContext._jsc.hadoopConfiguration().set("fs.s3a.access.key", cfg.username)
    spark.sparkContext._jsc.hadoopConfiguration().set("fs.s3a.secret.key", cfg.password)
    spark.sparkContext._jsc.hadoopConfiguration().set(
        "fs.s3a.endpoint", f"http://{cfg.host}"
    )
    spark.sparkContext._jsc.hadoopConfiguration().set(
        "spark.hadoop.fs.s3a.impl", "org.apache.hadoop.fs.s3a.S3AFileSystem"
    )
    spark.sparkContext._jsc.hadoopConfiguration().set(
        "spark.hadoop.fs.s3a.path.style.access", "true"
    )
    spark.sparkContext._jsc.hadoopConfiguration().set(
        "fs.s3a.multipart.size", "104857600"
    )
    return spark


def bucket_prefix(root):
    bucket, _, _, prefix = split_zfs(root)
    return bucket, prefix


def get_file(src):
    from miniofs import client

    try:
        assert not src.endswith("/")
        file_name = src.split("/")[-1]
        assert file_name.__contains__(".")
        bucket, prefix = bucket_prefix(src)
        for obj in client.list_objects(bucket, recursive=True, prefix=prefix):
            try:
                assert obj.object_name.endswith(file_name)
                return obj
            except AssertionError:
                pass
    except AssertionError:
        return None
    return None


def get_dir(src):
    from miniofs import client

    src = map_uri(src)
    try:
        file_name = src.split("/")[-1]
        assert not file_name.__contains__(".")
        src = src[:-1] if src.endswith("/") else src
        bucket, prefix = bucket_prefix(src)
        objs = [f for f in client.list_objects(bucket, recursive=False, prefix=prefix)]
        _dir = [f for f in objs if f.object_name.endswith(f"{fs.name(src)}/")]
        assert len(_dir) == 1
        return _dir[0]
    except AssertionError:
        return None


def listparents(*args, **kwargs):
    return list(set([fs.parent(f) for f in listfiles(*args, **kwargs)]))


def listdirs(src):
    return listparents(src)


def list_empty(src):
    group, partition = split_zfs(src)[1:3]
    dirs = [
        f"{__ZFS_URI__}://{group.upper()}/{partition.lower()}/{obj.object_name}"
        for obj in list_objects(src)
        if obj.is_dir
    ]
    return dirs


def list_objects(src):
    from miniofs import client

    try:
        assert get_dir(src) is not None
    except:
        return []
    src = PathURI(src)
    bucket, prefix = bucket_prefix(src)
    objs = [
        obj
        for obj in client.list_objects(bucket, recursive=True, prefix=prefix)
        if obj.object_name.startswith(f"{prefix}")
    ]
    return objs


def heal(src):
    return [mv(d, f"{__ZFS_URI__}/trash", dry_run=False) for d in list_empty(src)]


# def list_objects(src):
#     from miniofs import client

#     src = src[:-1] if src.endswith("/") else src
#     if len(src.split("zfs://")[1].split("/")) > 2:
#         assert get_dir(src) is not None
#     bucket, group, partition, prefix = split_zfs(src)
#     prefix2 = f"{prefix}/" if not prefix.endswith("/") else prefix
#     files = [
#         f"zfs://{group.upper()}/{partition.lower()}/{obj.object_name}"
#         for obj in client.list_objects(bucket, recursive=recursive, prefix=prefix)
#         if obj.object_name.startswith(f"{prefix2}")
#     ]
#     retyrb fukes


def listfiles(src, patterns=[], uri=True, absolute=False):
    src = f"{src}/" if not src.endswith("/") else src
    group, partition = split_zfs(src)[1:3]
    files = [
        f"{__ZFS_URI__}://{group.upper()}/{partition.lower()}/{obj.object_name}"
        for obj in list_objects(src)
        if not obj.is_dir
    ]

    def check_conditions(cnds, f):
        for c in cnds:
            try:
                assert re.search(c, f) is None
            except AssertionError:
                return True
        return False

    if len(patterns) > 0:
        files = [f for f in files if check_conditions(patterns, f)]
    if absolute:
        return [Path(f) for f in files]
    elif uri:
        return [PathURI(f) for f in files]
    else:
        return [map_uri(f) for f in files]


# def listfiles(src, patterns=[], recursive=True, uri=True):
#     from miniofs import client

#     src = src[:-1] if src.endswith("/") else src
#     if len(src.split("zfs://")[1].split("/")) > 2:
#         assert get_dir(src) is not None
#     bucket, group, partition, prefix = split_zfs(src)
#     prefix2 = f"{prefix}/" if not prefix.endswith("/") else prefix
#     files = [
#         f"zfs://{group.upper()}/{partition.lower()}/{obj.object_name}"
#         for obj in client.list_objects(bucket, recursive=recursive, prefix=prefix)
#         if obj.object_name.startswith(f"{prefix2}")
#     ]

#     def check_conditions(cnds, f):
#         for c in cnds:
#             try:
#                 assert re.search(c, f) is None
#             except AssertionError:
#                 return True
#         return False

#     if len(patterns) > 0:
#         files = [f for f in files if check_conditions(patterns, f)]
#     if not uri:
#         from miniofs import Path

#         files = [Path(f) for f in files]
#     return files

def download_file(file, filestore=f"/{__FILESTORE__}"):
    from miniofs import client

    bucket, object_name = bucket_prefix(file)
    output_file = os.path.join(f"{filestore}/{bucket}", object_name)
    os.makedirs(parent(output_file), exist_ok=True)
    client.fget_object(
        bucket,
        object_name,
        output_file,
    )
    return output_file


def download_files(root, patterns=[], filestore=f"/{__FILESTORE__}"):
    files = listfiles(root, patterns)
    return [
        download_file(file, filestore=filestore)
        for file in tqdm(
            files, total=len(files), desc=f"Downloading objects to {filestore}"
        )
    ]


# def split_object_name(file, filestore="/FileStore"):
#     splits = file.split(filestore)[1].split("/")[1:]
#     bucket = splits[0]
#     object_name = "/".join(splits[1:])
#     return bucket, object_name


def upload(file, zfs_path):
    splits = zfs_path.split(f"{__ZFS_URI__}://")[1].split("/")
    group = splits[0].lower()
    partition = splits[1].lower()
    suffix = "/".join(splits[2:])
    # from miniofs import client

    # bucket, object_name = split_object_name(file, filestore=filestore)
    # client.fput_object(
    #     bucket,
    #     object_name,
    #     file,
    # )
    # return f"zfs://{bucket}/{object_name}"
    command = f"mc cp -r {file} {__ZFS_URI__}/{partition}-{group}/{suffix}"
    os.system(command)


def split_zfs(src):
    src = PathURI(src)
    splits = src.split(f"{__ZFS_URI__}://")[1].split("/")
    group = splits[0].lower()
    partition = splits[1].lower()
    suffix = "/".join(splits[2:])
    # suffix = suffix if not suffix.endswith("/") else suffix[:-1]
    return f"{partition}-{group}", group, partition, suffix


def map_uri(src):
    bucket, _, _, suffix = split_zfs(src)
    return f"zfs/{bucket}/{suffix}"


def mv(src, dst, dry_run=False, verbose=False):
    # command = f"nohup mc mv -q -r {map_uri(src)} {map_uri(dst)} >/tmp/.miniofs.out 2>&1 &"
    src = map_uri(src) if not src.startswith("zfs/") else src
    dst = map_uri(dst) if not dst.startswith("zfs/") else dst

    command = f"mc mv -q -r {src} {dst} >> /tmp/.miniofs.out"
    print(command) if verbose else None
    os.system(command) if not dry_run else None
    return command


def exists(src):
    return (get_dir(src) is not None) | (get_file(src) is not None)


# def upload_files(files, filestore="/FileStore", overwrite=True):
#     def _upload_file(file):
#         if overwrite:
#             return upload_file(file, filestore=filestore)
#         elif not exists(file, filestore=filestore):
#             return upload_file(file, filestore=filestore)
#         else:
#             bucket, object_name = split_object_name(file, filestore=filestore)
#             return f"zfs://{bucket}/{object_name}"

#     return [
#         _upload_file(file)
#         for file in tqdm(
#             files, total=len(files), desc=f"Uploading objects from {filestore}"
#         )
#     ]
if __name__ == "__main__":
    from miniofs import Path, listfiles

    listfiles("zfs://KIRON/var/csv/", recursive=True, patterns=[".csv"], uri=False)
