import os
from kubernetes import client, config, watch
from kubevirtclient import KubeVirtClient

try: 
    from rich import print
except ImportError:
    pass


class VirtualServerClient:

    GROUP = 'virtualservers.coreweave.com'
    VERSION = 'v1alpha1'
    PLURAL = 'virtualservers'
    EXPECTED_CONDITIONS = {
        'Stopped': { # Outerbounds calls this hibernating
            'reason': 'VirtualServerStopped',
            'status': 'False',
            'type': 'Ready',
        },
        'Ready': {
            'reason': 'VirtualServerReady',
            'status': 'True',
            'type': 'Ready',
        },
        'Terminating': {
            'reason': 'Terminating',
            'status': 'False',
            'type': 'Ready',
        }
    }

    def __init__(self, kubeconfig_path=None):
        config.kube_config.load_kube_config(kubeconfig_path)

        self.api = client.CustomObjectsApi()
        self.kubevirt_api = KubeVirtClient(kubeconfig_path=kubeconfig_path)

    def create(self, manifest): 
        
        if not manifest['metadata']:
            raise TypeError('`metadata` is required in manifest for VirtualServer creation.')

        namespace = manifest['metadata']['namespace']
        name = manifest['metadata']['name']
        if not namespace or not name:
            raise ValueError('`metadata.namespace` and `metadata.name` are required in manifest for VirtualServer creation.')
        return self.api.create_namespaced_custom_object(
            group=VirtualServerClient.GROUP,
            version=VirtualServerClient.VERSION,
            namespace=namespace,
            plural=VirtualServerClient.PLURAL,
            body=manifest
        )

    def update(self, manifest):
        if not manifest['metadata']:
            raise TypeError('`metadata` is required in manifest for VirtualServer update.')
        
        namespace = manifest['metadata']['namespace']
        name = manifest['metadata']['name']
        if not namespace or not name:
            raise ValueError('`metadata.namespace` and `metadata.name` are required in manifest for VirtualServer update.')
        return self.api.replace_namespaced_custom_object(
            group=VirtualServerClient.GROUP,
            version=VirtualServerClient.VERSION,
            namespace=namespace,
            plural=VirtualServerClient.PLURAL,
            name=name,
            body=manifest
        )

    @staticmethod
    def match_condition(condition, expected_status):
        if expected_status in VirtualServerClient.EXPECTED_CONDITIONS:
            _c = VirtualServerClient.EXPECTED_CONDITIONS[expected_status]
            if 'reason' in condition and _c['reason'] == condition['reason'] and \
                'status' in condition and _c['status'] == condition['status'] and \
                'type' in condition and _c['type'] == condition['type']:
                return expected_status
        return None

    def ready(self, namespace, name, expected_status='Ready', progress=None):

        w = watch.Watch()
        kwargs = {
            'watch': True,
            'field_selector': f'metadata.name={name}'
        }
        ready_condition = None

        def _wait():

            result = {}
            for event in w.stream(
                self.api.list_namespaced_custom_object, 
                VirtualServerClient.GROUP, 
                VirtualServerClient.VERSION, 
                namespace, 
                VirtualServerClient.PLURAL, 
                **kwargs
            ):
                
                status = event['object'].get('status', {})
                if event['type'] == 'DELETE':
                    ready_condition = 'Deleted'
                    print(f'[bold red]{name}[/bold red] has been [red]deleted[/red].')
                    w.stop()
                    break
                if 'conditions' not in status:
                    continue

                ready_condition = VirtualServerClient.match_condition(status['conditions'][0], expected_status)
                if not ready_condition:
                    continue
                elif ready_condition == 'Ready':
                    print(f'[green]{name} is ready![/green] External IP: [red]{status['network']['externalIP']}[/red]')
                    result = {'ip': status['network']['externalIP']}
                    w.stop()
                    return result
                elif ready_condition == 'Stopped' or ready_condition == 'Terminating':
                    print(f'[red]{name}[/red] but is [red]{ready_condition}[/red].')
                    w.stop()

        if progress:
            progress.add_task(description=f'Waiting for VirtualServer {name} in namespace {namespace} to be {expected_status}...')
            return _wait()
        else:
            print(f'Waiting for VirtualServer {name} in namespace {namespace} to be {expected_status}...')
            return _wait()


    def get(self, namespace, name, pretty='true'):
        return self.api.get_namespaced_custom_object(
            group=VirtualServerClient.GROUP,
            version=VirtualServerClient.VERSION,
            namespace=namespace,
            plural=VirtualServerClient.PLURAL,
            field_selector=f'metadata.name={name}',
            pretty=pretty
        )

    def list(self, namespace, pretty='true'):
        return self.api.list_namespaced_custom_object(
            group=VirtualServerClient.GROUP,
            version=VirtualServerClient.VERSION,
            namespace=namespace,
            plural=VirtualServerClient.PLURAL,
            pretty=pretty
        )
    
    def delete(self, namespace, name, body=None):
        return self.api.delete_namespaced_custom_object(
            group=VirtualServerClient.GROUP,
            version=VirtualServerClient.VERSION,
            namespace=namespace,
            plural=VirtualServerClient.PLURAL,
            name=name
        )

def get_manifest(
    name,
    namespace,
    users, # list of Dict with {'username': ...} and {'password': ...} or {'sshpublickey': ...}
    region='ORD1',
    network_config={
        'directAttachLoadBalancerIP': True,
        'disableK8sNetworking': False,
        'dnsPolicy': 'ClusterFirst',
        'headless': False,
        'public': True
    },
    n_cpu=4,
    n_gpu=1,
    cpu_type=None,
    gpu_type=None,
    memory='32Gi',
    disk_size='300Gi',
    storage_source=None
):

    storage_class_name = 'block-nvme-%s' % region.lower()
    if not storage_source:
        storage_source = {
            'pvc': {
                'name': 'ubuntu2004-nvidia-550-54-15-1-docker-master-20240402-ord1',
                'namespace': 'vd-images'
            }
        }

    m = {
        'apiVersion': f'{VirtualServerClient.GROUP}/{VirtualServerClient.VERSION}',
        'kind': 'VirtualServer',
        'metadata': {
            'name': name,
            'namespace': namespace,
        },
        'spec': {
            'region': region,
            'os': {
                'type': 'linux'
            },
            'resources': {
                'cpu': {
                    'count': n_cpu
                },
                'memory': memory
            },
            'storage': {
                'root': {
                    'accessMode': 'ReadWriteOnce',
                    'size': disk_size,
                    'storageClassName': storage_class_name,
                    'source': storage_source
                }
            }
        },
        'network': network_config,
        'users': users,
        'cloudInit': '{}\n',
        'runStrategy': 'RerunOnFailure',
        'initializeRunning': True
    }

    if n_gpu > 0:
        m['spec']['resources']['gpu'] = {
            'count': n_gpu
        }
    # NOTE: cannot specify both.
    if cpu_type:
        m['spec']['resources']['cpu']['type'] = cpu_type
    elif gpu_type:
        m['spec']['resources']['gpu']['type'] = gpu_type

    return m