#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
"""
Databricks hook.

This hook enable the submitting and running of jobs to the Databricks platform. Internally the
operators talk to the ``api/2.0/jobs/runs/submit``
`endpoint <https://docs.databricks.com/api/latest/jobs.html#runs-submit>`_.
"""
from time import sleep
from urllib.parse import urlparse

import requests
from requests import PreparedRequest, exceptions as requests_exceptions
from requests.auth import AuthBase

from airflow import __version__
from airflow.exceptions import AirflowException
from airflow.hooks.base import BaseHook

RESTART_CLUSTER_ENDPOINT = ("POST", "api/2.0/clusters/restart")
START_CLUSTER_ENDPOINT = ("POST", "api/2.0/clusters/start")
TERMINATE_CLUSTER_ENDPOINT = ("POST", "api/2.0/clusters/delete")

RUN_NOW_ENDPOINT = ('POST', 'api/2.0/jobs/run-now')
SUBMIT_RUN_ENDPOINT = ('POST', 'api/2.0/jobs/runs/submit')
GET_RUN_ENDPOINT = ('GET', 'api/2.0/jobs/runs/get')
CANCEL_RUN_ENDPOINT = ('POST', 'api/2.0/jobs/runs/cancel')
USER_AGENT_HEADER = {'user-agent': f'airflow-{__version__}'}

INSTALL_LIBS_ENDPOINT = ('POST', 'api/2.0/libraries/install')
UNINSTALL_LIBS_ENDPOINT = ('POST', 'api/2.0/libraries/uninstall')


class RunState:
    """Utility class for the run state concept of Databricks runs."""

    def __init__(self, life_cycle_state: str, result_state: str, state_message: str) -> None:
        self.life_cycle_state = life_cycle_state
        self.result_state = result_state
        self.state_message = state_message

    @property
    def is_terminal(self) -> bool:
        """True if the current state is a terminal state."""
        if self.life_cycle_state not in RUN_LIFE_CYCLE_STATES:
            raise AirflowException(
                (
                    'Unexpected life cycle state: {}: If the state has '
                    'been introduced recently, please check the Databricks user '
                    'guide for troubleshooting information'
                ).format(self.life_cycle_state)
            )
        return self.life_cycle_state in ('TERMINATED', 'SKIPPED', 'INTERNAL_ERROR')

    @property
    def is_successful(self) -> bool:
        """True if the result state is SUCCESS"""
        return self.result_state == 'SUCCESS'

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RunState):
            return NotImplemented
        return (
            self.life_cycle_state == other.life_cycle_state
            and self.result_state == other.result_state
            and self.state_message == other.state_message
        )

    def __repr__(self) -> str:
        return str(self.__dict__)


class DatabricksHook(BaseHook):
    """
    Interact with Databricks.

    :param databricks_conn_id: Reference to the :ref:`Databricks connection <howto/connection:databricks>`.
    :type databricks_conn_id: str
    :param timeout_seconds: The amount of time in seconds the requests library
        will wait before timing-out.
    :type timeout_seconds: int
    :param retry_limit: The number of times to retry the connection in case of
        service outages.
    :type retry_limit: int
    :param retry_delay: The number of seconds to wait between retries (it
        might be a floating point number).
    :type retry_delay: float
    """

    conn_name_attr = 'databricks_conn_id'
    default_conn_name = 'databricks_default'
    conn_type = 'databricks'
    hook_name = 'Databricks'

    def __init__(
        self,
        databricks_conn_id: str = default_conn_name,
        timeout_seconds: int = 180,
        retry_limit: int = 3,
        retry_delay: float = 1.0,
    ) -> None:
        super().__init__()
        self.databricks_conn_id = databricks_conn_id
        self.databricks_conn = None
        self.timeout_seconds = timeout_seconds
        if retry_limit < 1:
            raise ValueError('Retry limit must be greater than equal to 1')
        self.retry_limit = retry_limit
        self.retry_delay = retry_delay

    @staticmethod
    def _parse_host(host: str) -> str:
        """
        The purpose of this function is to be robust to improper connections
        settings provided by users, specifically in the host field.

        For example -- when users supply ``https://xx.cloud.databricks.com`` as the
        host, we must strip out the protocol to get the host.::

            h = DatabricksHook()
            assert h._parse_host('https://xx.cloud.databricks.com') == \
                'xx.cloud.databricks.com'

        In the case where users supply the correct ``xx.cloud.databricks.com`` as the
        host, this function is a no-op.::

            assert h._parse_host('xx.cloud.databricks.com') == 'xx.cloud.databricks.com'

        """
        urlparse_host = urlparse(host).hostname
        if urlparse_host:
            # In this case, host = https://xx.cloud.databricks.com
            return urlparse_host
        else:
            # In this case, host = xx.cloud.databricks.com
            return host

    def _do_api_call(self, endpoint_info, json):
        """
        Utility function to perform an API call with retries

        :param endpoint_info: Tuple of method and endpoint
        :type endpoint_info: tuple[string, string]
        :param json: Parameters for this API call.
        :type json: dict
        :return: If the api call returns a OK status code,
            this function returns the response in JSON. Otherwise,
            we throw an AirflowException.
        :rtype: dict
        """
        method, endpoint = endpoint_info

        self.databricks_conn = self.get_connection(self.databricks_conn_id)

        if 'token' in self.databricks_conn.extra_dejson:
            self.log.info('Using token auth. ')
            auth = _TokenAuth(self.databricks_conn.extra_dejson['token'])
            if 'host' in self.databricks_conn.extra_dejson:
                host = self._parse_host(self.databricks_conn.extra_dejson['host'])
            else:
                host = self.databricks_conn.host
        else:
            self.log.info('Using basic auth. ')
            auth = (self.databricks_conn.login, self.databricks_conn.password)
            host = self.databricks_conn.host

        url = f'https://{self._parse_host(host)}/{endpoint}'

        if method == 'GET':
            request_func = requests.get
        elif method == 'POST':
            request_func = requests.post
        elif method == 'PATCH':
            request_func = requests.patch
        else:
            raise AirflowException('Unexpected HTTP Method: ' + method)

        attempt_num = 1
        while True:
            try:
                response = request_func(
                    url,
                    json=json if method in ('POST', 'PATCH') else None,
                    params=json if method == 'GET' else None,
                    auth=auth,
                    headers=USER_AGENT_HEADER,
                    timeout=self.timeout_seconds,
                )
                response.raise_for_status()
                return response.json()
            except requests_exceptions.RequestException as e:
                if not _retryable_error(e):
                    # In this case, the user probably made a mistake.
                    # Don't retry.
                    raise AirflowException(
                        f'Response: {e.response.content}, Status Code: {e.response.status_code}'
                    )

                self._log_request_error(attempt_num, e)

            if attempt_num == self.retry_limit:
                raise AirflowException(
                    f'API requests to Databricks failed {self.retry_limit} times. Giving up.'
                )

            attempt_num += 1
            sleep(self.retry_delay)

    def _log_request_error(self, attempt_num: int, error: str) -> None:
        self.log.error('Attempt %s API Request to Databricks failed with reason: %s', attempt_num, error)

    def run_now(self, json: dict) -> str:
        """
        Utility function to call the ``api/2.0/jobs/run-now`` endpoint.

        :param json: The data used in the body of the request to the ``run-now`` endpoint.
        :type json: dict
        :return: the run_id as a string
        :rtype: str
        """
        response = self._do_api_call(RUN_NOW_ENDPOINT, json)
        return response['run_id']

    def submit_run(self, json: dict) -> str:
        """
        Utility function to call the ``api/2.0/jobs/runs/submit`` endpoint.

        :param json: The data used in the body of the request to the ``submit`` endpoint.
        :type json: dict
        :return: the run_id as a string
        :rtype: str
        """
        response = self._do_api_call(SUBMIT_RUN_ENDPOINT, json)
        return response['run_id']

    def get_run_page_url(self, run_id: str) -> str:
        """
        Retrieves run_page_url.

        :param run_id: id of the run
        :return: URL of the run page
        """
        json = {'run_id': run_id}
        response = self._do_api_call(GET_RUN_ENDPOINT, json)
        return response['run_page_url']

    def get_job_id(self, run_id: str) -> str:
        """
        Retrieves job_id from run_id.

        :param run_id: id of the run
        :type run_id: str
        :return: Job id for given Databricks run
        """
        json = {'run_id': run_id}
        response = self._do_api_call(GET_RUN_ENDPOINT, json)
        return response['job_id']

    def get_run_state(self, run_id: str) -> RunState:
        """
        Retrieves run state of the run.

        :param run_id: id of the run
        :return: state of the run
        """
        json = {'run_id': run_id}
        response = self._do_api_call(GET_RUN_ENDPOINT, json)
        state = response['state']
        life_cycle_state = state['life_cycle_state']
        # result_state may not be in the state if not terminal
        result_state = state.get('result_state', None)
        state_message = state['state_message']
        return RunState(life_cycle_state, result_state, state_message)

    def cancel_run(self, run_id: str) -> None:
        """
        Cancels the run.

        :param run_id: id of the run
        """
        json = {'run_id': run_id}
        self._do_api_call(CANCEL_RUN_ENDPOINT, json)

    def restart_cluster(self, json: dict) -> None:
        """
        Restarts the cluster.

        :param json: json dictionary containing cluster specification.
        """
        self._do_api_call(RESTART_CLUSTER_ENDPOINT, json)

    def start_cluster(self, json: dict) -> None:
        """
        Starts the cluster.

        :param json: json dictionary containing cluster specification.
        """
        self._do_api_call(START_CLUSTER_ENDPOINT, json)

    def terminate_cluster(self, json: dict) -> None:
        """
        Terminates the cluster.

        :param json: json dictionary containing cluster specification.
        """
        self._do_api_call(TERMINATE_CLUSTER_ENDPOINT, json)

    def install(self, json: dict) -> None:
        """
        Install libraries on the cluster.

        Utility function to call the ``2.0/libraries/install`` endpoint.

        :param json: json dictionary containing cluster_id and an array of library
        :type json: dict
        """
        self._do_api_call(INSTALL_LIBS_ENDPOINT, json)

    def uninstall(self, json: dict) -> None:
        """
        Uninstall libraries on the cluster.

        Utility function to call the ``2.0/libraries/uninstall`` endpoint.

        :param json: json dictionary containing cluster_id and an array of library
        :type json: dict
        """
        self._do_api_call(UNINSTALL_LIBS_ENDPOINT, json)


def _retryable_error(exception) -> bool:
    return (
        isinstance(exception, (requests_exceptions.ConnectionError, requests_exceptions.Timeout))
        or exception.response is not None
        and exception.response.status_code >= 500
    )


RUN_LIFE_CYCLE_STATES = ['PENDING', 'RUNNING', 'TERMINATING', 'TERMINATED', 'SKIPPED', 'INTERNAL_ERROR']


class _TokenAuth(AuthBase):
    """
    Helper class for requests Auth field. AuthBase requires you to implement the __call__
    magic function.
    """

    def __init__(self, token: str) -> None:
        self.token = token

    def __call__(self, r: PreparedRequest) -> PreparedRequest:
        r.headers['Authorization'] = 'Bearer ' + self.token
        return r
