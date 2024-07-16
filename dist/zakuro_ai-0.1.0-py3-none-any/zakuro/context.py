# -*- coding: utf-8 -*-

# from ray import (init, 
#                  remote, 
#                  get, 
#                  is_initialized,
#                  serve, 
#                  ClientBuilder)

# from .functional import *
# from zakuro.nn import load

from distributed import Client

from dataclasses import dataclass, field
from functools import cached_property
from zakuro.var import __ZAKURO_URI__, __DASK__, __SPARK__
@dataclass
class Context:
    """
    Represents the context for executing tasks on different backends.

    Attributes:
        url (str): The URL of the backend.
        backend_name (str): The name of the backend (default: __DASK__).

    Methods:
        _cdata: Returns the backend client or session.
        workers: Returns a list of worker nodes in the backend.
        get_worker: Returns a worker object for the specified worker node.
        find: Finds a worker object based on the given IP address.
        submit: Submits a task to the backend for execution.
    """

    url: str
    backend_name: str = __DASK__

    @cached_property
    def _cdata(self):
        uri = f"{__ZAKURO_URI__}://"
        url_splits = self.url.split(uri)
        if self.backend_name == __DASK__:
            assert self.url.startswith(uri)
            url = f"tcp://{url_splits[1]}:8786"
            return Client(url)
        elif self.backend_name == __SPARK__:
            url = f"spark://{self.url.split(uri)[1]}:7077"
            from pyspark.sql import SparkSession
            return SparkSession.builder.config(
                "spark.driver.memory", "15g"
            ).master(url).getOrCreate()
        else:
            raise Exception

    @property
    def workers(self):
        """
        Returns a list of worker nodes in the backend.

        Returns:
            list: A list of worker nodes.
        """
        assert self.backend_name == __DASK__
        return list(self._cdata.scheduler_info()['workers'].keys())

    def get_worker(self, worker):
        """
        Returns a worker object for the specified worker node.

        Args:
            worker: The worker node or worker object.

        Returns:
            Worker: The worker object.
        """
        assert self.backend_name == __DASK__
        from zakuro.worker import Worker
        if type(worker) == Worker:
            return worker
        else:
            return Worker(worker)

    def find(self, ip):
        """
        Finds a worker object based on the given IP address.

        Args:
            ip (str): The IP address to search for.

        Returns:
            Worker: The worker object matching the IP address, or None if not found.
        """
        from zakuro.worker import Worker
        for w in self.workers:
            if w.__contains__(ip):
                return Worker(w)

    def submit(self, *args, **kwargs):
        """
        Submits a task to the backend for execution.

        Args:
            *args: Positional arguments to be passed to the task.
            **kwargs: Keyword arguments to be passed to the task.

        Returns:
            object: The result of the task execution.
        """
        assert self.backend_name == __DASK__
        return self._cdata.submit(*args, **kwargs)