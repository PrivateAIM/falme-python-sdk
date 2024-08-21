import time
from io import BytesIO
from enum import Enum
import asyncio

from typing import Any, Optional, Type

from flame import FlameCoreSDK

from flame.schemas.star.aggregator_client import Aggregator
from flame.schemas.star.analyzer_client import Analyzer


class _ERROR_MESSAGES(Enum):
    IS_ANALYZER = 'Node is configured as analyzer. Unable to execute command associated to aggregator.'
    IS_AGGREGATOR = 'Node is configured as aggregator. Unable to execute command associated to analyzer.'
    IS_INCORRECT_CLASS = 'The object/class given is incorrect, e.g. is not correctly implementing/inheriting the ' \
                         'intended template class.'


class StarModel:
    flame: FlameCoreSDK

    aggregator: Optional[Aggregator]
    analyzer: Optional[Analyzer]

    def __init__(self) -> None:
        self.flame = FlameCoreSDK()

    def is_aggregator(self) -> bool:
        return self.flame.get_role() == 'aggregator'

    def is_analyzer(self) -> bool:
        return self.flame.get_role() == 'default'

    def start_aggregator(self, aggregator: Type[Aggregator] | Any, simple_analysis: bool = True) -> None:

        if self.is_aggregator():
            if isinstance(aggregator, Aggregator) or issubclass(aggregator, Aggregator):
                # init subclass, if not an object
                # (funfact: isinstance(class, Class) returns False, issubclass(object, Class) raises a TypeError)
                self.aggregator = aggregator if isinstance(aggregator, Aggregator) else aggregator(flame=self.flame)

                # Ready Check
                self._wait_until_partners_ready()

                while not self.converged():  # (**)
                    # Await number of responses reaching number of necessary nodes
                    node_response_dict = self.flame.await_and_return_responses(node_ids=self.aggregator.partner_node_ids,
                                                                               message_category='intermediate_results')
                    print(f"Node responses: {node_response_dict}")
                    if all([v for v in list(node_response_dict.values())]):
                        node_results = [response[-1].body['result'] for response in list(node_response_dict.values())
                                        if response is not None]
                        print(f"Node results received: {node_results}")

                        # Aggregate results
                        aggregated_res, converged = self.aggregator.aggregate(node_results=node_results,
                                                                              simple_analysis=simple_analysis)
                        print(f"Aggregated results: {aggregated_res}")

                        # If converged send aggregated result over StorageAPI to Hub
                        if converged:
                            print("Submitting final results...", end='')
                            response = self.flame.submit_final_result(BytesIO(str(aggregated_res).encode('utf8')))
                            print(f"success (response={response})")
                            self.flame.analysis_finished()  # LOOP BREAK

                        # Else send aggregated results to MinIO for analyzers, loop back to (**)
                        else:
                            self.flame.send_message(self.aggregator.partner_node_ids,
                                                    'aggregated_results',
                                                    {'result': str(aggregated_res)})
                self.aggregator.node_finished()
            else:
                raise BrokenPipeError(_ERROR_MESSAGES.IS_INCORRECT_CLASS.value)
        else:
            raise BrokenPipeError(_ERROR_MESSAGES.IS_ANALYZER.value)

    def start_analyzer(self, analyzer: Type[Analyzer] | Any, query: str | list[str], simple_analysis: bool = True) -> None:

        if self.is_analyzer():
            if isinstance(analyzer, Analyzer) or issubclass(analyzer, Analyzer):
                # init subclass, if not an object
                # (funfact: isinstance(class, Class) returns False, issubclass(object, Class) raises a TypeError)
                self.analyzer = analyzer if isinstance(analyzer, Analyzer) else analyzer(flame=self.flame)

                aggregator_id = self.flame.get_aggregator_id()

                # Ready Check
                self._wait_until_partners_ready()

                # Get data
                data = asyncio.run(self._get_data(query=query))
                print(f"Data extracted: {data}")

                aggregator_results = None
                converged = False
                # Check converged status on Hub
                while not self.converged():  # (**)
                    if not converged:
                        # Analyze data
                        analyzer_res, converged = self.analyzer.analyze(data=data,
                                                                        aggregator_results=aggregator_results,
                                                                        simple_analysis=simple_analysis)
                        # Send result to (MinIO for) aggregator
                        self.flame.send_message(receivers=[aggregator_id],
                                                message_category='intermediate_results',
                                                message={'result': str(analyzer_res)})
                    if (not self.converged()) and (not converged):
                        # Check for aggregated results
                        aggregator_results = self.flame.await_and_return_responses(node_ids=[aggregator_id],
                                                                                   message_category='aggregated_results',
                                                                                   timeout=300)[aggregator_id][-1].body['result']
                self.analyzer.node_finished()
            else:
                raise BrokenPipeError(_ERROR_MESSAGES.IS_INCORRECT_CLASS.value)
        else:
            raise BrokenPipeError(_ERROR_MESSAGES.IS_AGGREGATOR.value)

    def _wait_until_partners_ready(self):
        if self.is_analyzer():
            aggregator_id = self.flame.get_aggregator_id()
            print("Awaiting contact with aggregator node...")
            received_list = []
            while aggregator_id not in received_list:
                time.sleep(1)

                received_list, _ = self.flame.send_message(receivers=[aggregator_id],
                                                           message_category='ready_check',
                                                           message={},
                                                           timeout=120)
            if aggregator_id not in received_list:
                raise BrokenPipeError("Could not contact aggregator")

            print("Awaiting contact with aggregator node...success")
        else:
            analyzer_ids = self.flame.get_participant_ids()
            latest_num_responses, num_responses = (-1, 0)
            while True:
                time.sleep(1)

                if latest_num_responses < num_responses:
                    latest_num_responses = num_responses
                    print(f"Awaiting contact with analyzer nodes...({num_responses}/{len(analyzer_ids)})")

                received_list, _ = self.flame.send_message(receivers=analyzer_ids,
                                                           message_category='ready_check',
                                                           message={},
                                                           timeout=120)
                num_responses = len(received_list)
                if num_responses == len(analyzer_ids):
                    break

            print("Awaiting contact with analyzer nodes...success")

    async def _get_data(self, query: str | list[str]):
        if type(query) == str:
            response = await self.flame._data_api.data_clients.client.get(f"/{self.flame.config.project_id}/fhir/{query}",
                                                                          headers=[('Connection', 'close')])
            try:
                response.raise_for_status()
                return response.json()
            except:
                return False
        else:
            responses = {}
            for q in query:
                response = await self.flame._data_api.data_clients.client.get(f"/{self.flame.config.project_id}/fhir/{q}",
                                                                              headers=[('Connection', 'close')])
                try:
                    response.raise_for_status()
                    responses[q] = response.json()
                except:
                    print(f"Failed to extract data from fhir dataset with query={q}")
                    pass
            return responses if responses else False

    def converged(self) -> bool:
        return self.flame.config.finished
