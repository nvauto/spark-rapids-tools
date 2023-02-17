# Copyright (c) 2023, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Implementation specific to EMR"""

import json
import os
from dataclasses import field, dataclass
from typing import Any

from spark_rapids_pytools.cloud_api.emr_job import EmrServerlessRapidsJob, EmrLocalRapidsJob
from spark_rapids_pytools.cloud_api.s3storage import S3StorageDriver
from spark_rapids_pytools.cloud_api.sp_types import PlatformBase, ClusterBase, CMDDriverBase, CloudPlatform, \
    ClusterState, SparkNodeType, ClusterNode, GpuHWInfo, SysInfo, GpuDevice
from spark_rapids_pytools.common.prop_manager import JSONPropertiesContainer, AbstractPropertiesContainer
from spark_rapids_pytools.common.utilities import Utils
from spark_rapids_pytools.pricing.emr_pricing import EMREc2PriceProvider
from spark_rapids_pytools.pricing.price_provider import SavingsEstimator
from spark_rapids_pytools.rapids.rapids_job import RapidsJobPropContainer, RapidsJob


@dataclass
class EMRPlatform(PlatformBase):
    """
    Represents the interface and utilities required by AWS EMR.
    Prerequisites:
    - install aws command lines (aws cli)
    - configure the aws
        - this may be done by region
    - aws has no staging available in the cluster properties.
    - gsutil is used to move data from/to storage
    """

    def set_offline_cluster(self, cluster_args: dict = None):
        pass

    @classmethod
    def get_spark_node_type_fromstring(cls, value) -> SparkNodeType:
        if value.upper() in ['TASK', 'CORE']:
            return SparkNodeType.WORKER
        return SparkNodeType.fromstring(value)

    @classmethod
    def process_raw_cluster_prop(cls, prop_container: AbstractPropertiesContainer) -> str:
        if prop_container.get_value_silent('Cluster'):
            _, prop_container.props = prop_container.props.popitem()
        return json.dumps(prop_container.props)

    def __post_init__(self):
        self.type_id = CloudPlatform.EMR
        super().__post_init__()

    def _construct_cli_object(self) -> CMDDriverBase:
        return EMRCMDDriver(timeout=0, cloud_ctxt=self.ctxt)

    def _install_storage_driver(self):
        self.storage = S3StorageDriver(self.cli)

    def _construct_cluster_from_props(self,
                                      cluster: str,
                                      props: str = None):
        return EMRCluster(self).set_connection(cluster_id=cluster, props=props)

    def migrate_cluster_to_gpu(self, orig_cluster):
        """
        given a cluster, convert it to run NVIDIA Gpu based on mapping instance types
        :param orig_cluster: the original cluster to migrate from
        :return: a new object cluster that supports GPU
        """
        gpu_cluster_ob = EMRCluster(self)
        gpu_cluster_ob.migrate_from_cluster(orig_cluster)
        return gpu_cluster_ob

    def validate_job_submission_args(self, submission_args: dict) -> dict:
        """
        process the job submission and return the final arguments to be used for the execution.
        :param submission_args: dictionary containing the job submission arguments
        :return: a dictionary with the processed arguments.
        """
        # TODO: verify that all arguments are valid
        valid_keys = ['execution-role-arn', 'application-id']
        for submit_arg in submission_args:
            if submit_arg not in valid_keys:
                raise RuntimeError(f'Invalid submission argument [{submit_arg}]. Accepted arguments: {valid_keys}.')
            if submit_arg == 'application-id' and submission_args.get(submit_arg) is None:
                # show a message that the appID is not passed
                self.cli.logger.warning('The EMR-Serverless application-ID is not set. '
                                        'Note that it is recommended to use a pre-existing SPARK EMR-Serverless '
                                        'application-id to reduce the overhead of initializing the job.')
        return submission_args

    def create_saving_estimator(self, source_cluster, target_cluster):
        emr_price_provider = EMREc2PriceProvider(region=self.cli.get_region())
        saving_estimator = EmrSavingsEstimator(price_provider=emr_price_provider,
                                               target_cluster=target_cluster,
                                               source_cluster=source_cluster)
        return saving_estimator

    def create_submission_job(self, job_prop: RapidsJobPropContainer, ctxt) -> RapidsJob:
        return EmrServerlessRapidsJob(prop_container=job_prop, exec_ctxt=ctxt)

    def create_local_submission_job(self, job_prop, ctxt) -> Any:
        return EmrLocalRapidsJob(prop_container=job_prop, exec_ctxt=ctxt)


@dataclass
class EMRCMDDriver(CMDDriverBase):
    """Represents the command interface that will be used by EMR"""

    def _list_inconsistent_configurations(self) -> list:
        incorrect_envs = super()._list_inconsistent_configurations()
        # check that private key file path is correct
        emr_pem_path = self.env_vars.get('keyPairPath')
        if emr_pem_path is not None:
            if not os.path.exists(emr_pem_path):
                incorrect_envs.append(f'Private key file path [{emr_pem_path}] does not exist. '
                                      'It is required to SSH on driver node.')
            else:
                # check valid extension
                if not (emr_pem_path.endswith('.pem') or emr_pem_path.endswith('ppk')):
                    incorrect_envs.append(f'Private key file path [{emr_pem_path}] should be ppk or pem format')
        else:
            incorrect_envs.append(
                f'Private key file path is not set. It is required to SSH on driver node. '
                f'Set {Utils.find_full_rapids_tools_env_key("KEY_PAIR_PATH")}')
        return incorrect_envs

    def pull_cluster_props_by_args(self, args: dict) -> str:
        aws_cluster_id = args.get('Id')
        cluster_name = args.get('cluster')
        if args.get('Id') is None:
            # use cluster name to get the cluster values
            # we need to get the cluster_id from the list command first.
            list_cmd_res = self.exec_platform_list_cluster_by_name(cluster_name)
            error_msg = f'Could not find EMR cluster {cluster_name} by name'
            if not list_cmd_res:
                raise RuntimeError(error_msg)
            # listed_cluster is json formatted string of array, but we need only the first entry
            # to read the clusterID
            cluster_headers: list = json.loads(list_cmd_res)
            if len(cluster_headers) == 0:
                raise RuntimeError(error_msg)
            existing_cluster = cluster_headers[0]
            aws_cluster_id = existing_cluster['Id']
        self.logger.debug('Cluster %s has an Id %s', cluster_name, aws_cluster_id)
        cluster_described = self.exec_platform_describe_cluster_by_id(aws_cluster_id)
        if cluster_described is not None:
            raw_prop_container = JSONPropertiesContainer(prop_arg=cluster_described, file_load=False)
            return EMRPlatform.process_raw_cluster_prop(raw_prop_container)
        return cluster_described

    def _build_ssh_cmd_prefix_for_node(self, node: ClusterNode) -> str:
        # get the pem file
        pem_file_path = self.env_vars.get('keyPairPath')
        prefix_args = ['ssh',
                       '-o StrictHostKeyChecking=no',
                       f'-i {pem_file_path}',
                       f'hadoop@{node.name}']
        return ' '.join(prefix_args)

    def _build_platform_describe_node_instance(self, node: ClusterNode) -> list:
        cmd_params = ['aws ec2 describe-instance-types',
                      '--region', f'{self.get_region()}',
                      '--instance-types', f'{node.instance_type}']
        return cmd_params

    def _build_platform_list_cluster(self,
                                     cluster,
                                     query_args: dict = None) -> list:
        # aws emr list-instances --cluster-id j-2DDF0Q87QOXON
        cmd_params = ['aws emr list-instances',
                      '--cluster-id',
                      f'{cluster.uuid}']
        if query_args is not None:
            for q_key in query_args:
                cmd_params.append(f'--{q_key}')
                cmd_params.append(f'{query_args.get(q_key)}')
        return cmd_params

    def exec_platform_list_cluster_by_name(self,
                                           cluster_name: str):
        list_cmd = f"aws emr list-clusters --query 'Clusters[?Name==`{cluster_name}`]'"
        return self.run_sys_cmd(list_cmd)

    def exec_platform_describe_cluster_by_id(self,
                                             cluster_id: str):
        describe_cmd = f'aws emr describe-cluster --cluster-id {cluster_id}'
        return self.run_sys_cmd(describe_cmd)


@dataclass
class InstanceGroup:
    """
    Holds information about instance groups
    """
    id: str  # group ID
    instance_type: str  # the machine type
    count: int  # Number of requested instances associated to that group
    market: str  # ON_DEMAND OR ON_SPOT
    group_type: str  # Master, TASK, or CORE
    spark_grp_type: SparkNodeType = field(default=None, init=False)  # map the group_type to Spark type.

    def __post_init__(self):
        self.spark_grp_type = EMRPlatform.get_spark_node_type_fromstring(self.group_type)


@dataclass
class Ec2Instance:
    """
    Holds information about instance groups
    """
    id: str
    ec2_instance_id: str
    dns_name: str
    group: InstanceGroup
    state: ClusterState  # RUNNING, TERMINATED..etc.


@dataclass
class EMRNode(ClusterNode):
    """
    Represents EMR cluster Node.
    We assume that all nodes are running on EC2 instances.
    """
    ec2_instance: Ec2Instance = field(default=None, init=False)

    def _pull_and_set_mc_props(self, cli=None):
        instance_description = cli.exec_platform_describe_node_instance(self)
        mc_description = json.loads(instance_description)['InstanceTypes'][0]
        self.mc_props = JSONPropertiesContainer(prop_arg=json.dumps(mc_description), file_load=False)

    def _set_fields_from_props(self):
        self.name = self.ec2_instance.dns_name
        self.instance_type = self.ec2_instance.group.instance_type

    def _pull_sys_info(self, cli=None) -> SysInfo:
        cpu_mem = self.mc_props.get_value('MemoryInfo', 'SizeInMiB')
        # TODO: should we use DefaultVCpus or DefaultCores
        num_cpus = self.mc_props.get_value('VCpuInfo', 'DefaultVCpus')
        return SysInfo(num_cpus=num_cpus, cpu_mem=cpu_mem)

    def _pull_gpu_hw_info(self, cli=None) -> GpuHWInfo or None:
        raw_gpus = self.mc_props.get_value_silent('GpuInfo')
        if raw_gpus is None:
            return None
        # TODO: we assume all gpus of the same type
        raw_gpu_arr = raw_gpus.get('Gpus')
        if raw_gpu_arr is None:
            return None
        raw_gpu = raw_gpu_arr[0]
        gpu_device = GpuDevice.fromstring(raw_gpu['Name'])
        gpu_cnt = raw_gpu['Count']
        gpu_mem = raw_gpu['MemoryInfo']['SizeInMiB']
        return GpuHWInfo(num_gpus=gpu_cnt,
                         gpu_device=gpu_device,
                         gpu_mem=gpu_mem)


@dataclass
class EMRCluster(ClusterBase):
    """
    Represents an instance of running cluster on EMR.
    """
    instance_groups: list = field(default=None, init=False)
    ec2_instances: list = field(default=None, init=False)

    def _process_loaded_props(self) -> None:
        """
        After loading the raw properties, perform any necessary processing to clean up the
        properties. We want to get rid of Cluster
        """
        if self.props.get_value_silent('Cluster') is not None:
            _, new_props = self.props.props.popitem()
            self.props.props = new_props

    def _init_connection(self, cluster_id: str = None,
                         props: str = None) -> dict:
        name = cluster_id
        if props is None:
            # we need to pull the properties from the platform
            props = self.cli.pull_cluster_props_by_args(args={'cluster': name, 'region': self.region})
        cluster_props = JSONPropertiesContainer(props, file_load=False)
        cluster_args = {
            'name': name,
            'props': cluster_props
        }
        return cluster_args

    def __create_ec2_list_by_group(self, group_arg):
        if isinstance(group_arg, InstanceGroup):
            group_obj = group_arg
            group_id = group_arg.id
        else:
            group_id = group_arg
            group_obj = None
        query_args = {'instance-group-id': group_id}
        raw_instance_list = self.cli.exec_platform_list_cluster_instances(self, query_args=query_args)
        instances_list = json.loads(raw_instance_list).get('Instances')
        ec2_instances = []
        for raw_inst in instances_list:
            parsed_state = raw_inst['Status']['State']
            ec2_instance = Ec2Instance(
                id=raw_inst['Id'],
                ec2_instance_id=raw_inst['Ec2InstanceId'],
                dns_name=raw_inst['PublicDnsName'],
                group=group_obj,
                state=ClusterState.fromstring(parsed_state)
            )
            ec2_instances.append(ec2_instance)
        return ec2_instances

    def find_matches_for_node(self) -> dict:
        mc_map = {}
        supported_gpus = self.platform.get_supported_gpus()
        for spark_node_type, node_list in self.nodes.items():
            if spark_node_type == SparkNodeType.MASTER:
                # skip
                self.cli.logger.debug('Skip converting Master nodes')
            else:
                for anode in node_list:
                    if anode.instance_type not in mc_map:
                        best_mc_match = anode.find_best_cpu_conversion(supported_gpus)
                        mc_map.update({anode.instance_type: best_mc_match})
        return mc_map

    def migrate_from_cluster(self, orig_cluster):
        self.name = orig_cluster.name
        self.uuid = orig_cluster.uuid
        self.zone = orig_cluster.zone
        self.state = orig_cluster.state
        group_cache = {}
        self.instance_groups = []
        self.ec2_instances = []
        # get the map of the instance types
        mc_type_map = orig_cluster.find_matches_for_node()
        # convert instances and groups
        # master groups should stay the same
        for curr_group in orig_cluster.instance_groups:
            if curr_group.spark_grp_type == SparkNodeType.MASTER:
                new_inst_grp = curr_group
            else:
                # convert the instance_type
                new_instance_type = mc_type_map.get(curr_group.instance_type, curr_group.instance_type)
                if new_instance_type == curr_group.instance_type:
                    new_inst_grp = curr_group
                else:
                    new_inst_grp = InstanceGroup(
                        id=curr_group.id,
                        instance_type=new_instance_type,
                        count=curr_group.count,
                        market=curr_group.market,
                        group_type=curr_group.group_type)
                group_cache.update({new_inst_grp.id: new_inst_grp})
            self.instance_groups.append(new_inst_grp)
        # convert the instances
        for ec2_inst in orig_cluster.ec2_instances:
            if ec2_inst.group.spark_grp_type == SparkNodeType.MASTER:
                new_group_obj = ec2_inst.group
            else:
                # get the new group
                new_group_obj = group_cache.get(ec2_inst.group.id)
            new_inst = Ec2Instance(
                id=ec2_inst.id,
                ec2_instance_id=ec2_inst.ec2_instance_id,
                dns_name=None,
                group=new_group_obj,
                state=ec2_inst.state)
            self.ec2_instances.append(new_inst)
        self.nodes = self.__create_node_from_instances()
        if bool(mc_type_map):
            # update the platform notes
            self.platform.update_ctxt_notes('nodeConversions', mc_type_map)

    def __create_node_from_instances(self):
        worker_nodes = []
        master_nodes = []
        for ec2_inst in self.ec2_instances:
            node_props = {
                'ec2_instance': ec2_inst
            }
            c_node = EMRNode.create_node(ec2_inst.group.spark_grp_type).set_fields_from_dict(node_props)
            c_node.fetch_and_set_hw_info(self.cli)
            if c_node.node_type == SparkNodeType.WORKER:
                worker_nodes.append(c_node)
            else:
                master_nodes.append(c_node)
        return {
            SparkNodeType.WORKER: worker_nodes,
            SparkNodeType.MASTER: master_nodes[0]
        }

    def _init_nodes(self):
        def process_cluster_group_list(inst_groups: list) -> list:
            instance_group_list = []
            for inst_grp in inst_groups:
                inst_group = InstanceGroup(
                    id=inst_grp['Id'],
                    instance_type=inst_grp['InstanceType'],
                    count=inst_grp['RequestedInstanceCount'],
                    market=inst_grp['Market'],
                    group_type=inst_grp['InstanceGroupType'],
                )
                instance_group_list.append(inst_group)
            return instance_group_list

        # get instance_groups from the cluster props.
        inst_grps = self.props.get_value('InstanceGroups')
        self.instance_groups = process_cluster_group_list(inst_grps)
        self.ec2_instances = []
        for curr_group in self.instance_groups:
            instances_list = self.__create_ec2_list_by_group(curr_group)
            self.ec2_instances.extend(instances_list)
        self.nodes = self.__create_node_from_instances()

    def _set_fields_from_props(self):
        super()._set_fields_from_props()
        self.uuid = self.props.get_value('Id')
        self.state = ClusterState.fromstring(self.props.get_value('Status', 'State'))
        self.zone = self.props.get_value('Ec2InstanceAttributes',
                                         'Ec2AvailabilityZone')
        if self.name is None:
            self.name = self.props.get_value('Name')

    def is_cluster_running(self) -> bool:
        acceptable_init_states = [
            ClusterState.RUNNING,
            ClusterState.STARTING,
            ClusterState.BOOTSTRAPPING,
            ClusterState.WAITING
        ]
        return self.state in acceptable_init_states

    def get_eventlogs_from_config(self):
        res_arr = []
        configs_list = self.props.get_value_silent('Configurations')
        for conf_item in configs_list:
            if conf_item['Classification'].startswith('spark'):
                conf_props = conf_item['Properties']
                if 'spark.eventLog.dir' in conf_props:
                    res_arr.append(conf_props['spark.eventLog.dir'])
        return res_arr


@dataclass
class EmrSavingsEstimator(SavingsEstimator):
    """
    A class that calculates the savings based on an EMR price provider
    """

    def _get_cost_per_cluster(self, cluster: EMRCluster):
        total_cost = 0.0
        for curr_group in cluster.instance_groups:
            ec2_unit_cost = self.price_provider.catalog.get_value('ec2', curr_group.instance_type)
            ec2_cost = ec2_unit_cost * curr_group.count
            emr_unit_cost = self.price_provider.catalog.get_value('emr', curr_group.instance_type)
            emr_cost = emr_unit_cost * curr_group.count
            total_cost += emr_cost + ec2_cost
        return total_cost

    def _setup_costs(self):
        # calculate target_cost
        self.target_cost = self._get_cost_per_cluster(self.target_cluster)
        self.source_cost = self._get_cost_per_cluster(self.source_cluster)
