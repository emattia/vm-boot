import os
import re
import time
import sys
import yaml
import typer
from typing import Optional 
from rich import print
from rich.prompt import Prompt, IntPrompt
from rich.progress import Progress, SpinnerColumn, TextColumn
from utils import fetch_ssh_public_key
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from kubernetes.client.rest import ApiException
from virtual_server import VirtualServerClient, get_manifest

# https://docs.coreweave.com/coreweave-kubernetes/node-types
GPU_TYPES = [
    'H100_NVLINNK_80GB',
    'H100_PCIE',
    'A100_NVLINK_80GB',
    'A100_NVLINK',
    'A100_PCIE_40GB',
    'A100_PCIE_80GB',
    'A40',
    'RTX_A6000',
    'RTX_A5000',
    'RTX_A4000',
    'Tesla_V100_NVLink',
    'Quadro_RTX_5000',
    'Quadro_RTX_4000'
]

CPU_TYPES = [
    'intel-xeon-v3',
    'intel-xeon-v4',
    'intel-xeon-icelake',
    'intel-xeon-scalable',
    'amd-epyc-milan',
    'amd-epyc-rome',
]


app = typer.Typer()
client = VirtualServerClient()

@app.command()
def hello(name: str):
    print(f"Hello {name}")

@app.command()
def create(
    manifest: str,
    name: str = 'test', 
    namespace: str = os.environ['KUBERNETES_NAMESPACE'],
    username: str = 'eddie',
    n_cpu: int = 4,
    cpu_type: str = 'amd-epyc-milan',
    n_gpu: int = 0,
    gpu_type: str = None,
    memory: str = '8Gi',
    disk_size: str = '128Gi',
    use_ssh_key: bool = True,
    ssh_dir_path: Optional[str] = None,
):

    """
    Create a VirtualServer with the given name and namespace on Coreweave.
    Find your namespace at https://cloud.coreweave.com/namespaces/.
    """

    if use_ssh_key:
        if ssh_dir_path:
            ssh_key_path = os.path.join(ssh_dir_path, 'id_rsa.pub')
        else:
            ssh_key_path = os.path.expanduser('~/.ssh/id_rsa.pub')
        ssh_public_key = fetch_ssh_public_key(ssh_key_path)
    else:
        raise ValueError('SSH key is required for VirtualServer creation.')


    if manifest:
        with open(manifest, 'r') as file:
            manifest = yaml.safe_load(file)
            
        namespace = os.environ['KUBERNETES_NAMESPACE']
        manifest['metadata']['namespace'] = namespace

        # HACK: assume only one user for now bc it's easier.
        username = Prompt.ask('Username', default=manifest['spec']['users'][0]['username'])
        manifest['spec']['users'][0]['username'] = username
        manifest['spec']['users'][0]['sshpublickey'] = ssh_public_key

        name = Prompt.ask('Virtual server name', default=manifest['metadata']['name'])
        manifest['metadata']['name'] = name

        region = Prompt.ask('Region', default=manifest['spec']['region'])
        manifest['spec']['region'] = region

        cpu_count = IntPrompt.ask(
            'Number of CPUs', 
            default=manifest['spec']['resources']['cpu']['count'],
            show_default=True
        )
        if 'gpu' in manifest['spec']['resources']:
            gpu_type = Prompt.ask('GPU Type', default=manifest['spec']['resources']['gpu']['type'], choices=GPU_TYPES)
            gpu_count = IntPrompt.ask(
                'Number of GPUs', 
                default=manifest['spec']['resources']['gpu']['count'],
                show_default=True
            )
            manifest['spec']['resources']['gpu']['type'] = gpu_type
            manifest['spec']['resources']['gpu']['count'] = gpu_count
        else:
            cpu_type = Prompt.ask('CPU Type', default=manifest['spec']['resources']['cpu']['type'], choices=CPU_TYPES)
            manifest['spec']['resources']['cpu']['type'] = cpu_type
        manifest['spec']['resources']['cpu']['count'] = cpu_count
        mem_parsed = int(manifest['spec']['resources']['memory'][:-2])
        memory = IntPrompt.ask('Memory', default=mem_parsed)
        manifest['spec']['resources']['memory'] = f'{memory}Gi'
        disk_parsed = int(manifest['spec']['storage']['root']['size'][:-2])
        disk_size = IntPrompt.ask('Disk Size', default=disk_parsed)
        manifest['spec']['storage']['root']['size'] = f'{disk_size}Gi'

    else:
        raise NotImplementedError('Manifest path not provided.')
        # TODO: this path is broken. Mostly cuz get_manifest is whack.
        # manifest = get_manifest(
        #     name,
        #     namespace,
        #     users,
        #     n_cpu=n_cpu,
        #     cpu_type=cpu_type,
        #     n_gpu=n_gpu,
        #     gpu_type=gpu_type,
        #     memory=memory,
        #     disk_size=disk_size
        # )
    result = client.create(manifest)

    print(f'Virtual server [bold red]{name}[/bold red] created in namespace [red]{namespace}[/red] :boom:')

    ### get IP address
    # print('Virtual server status: %s' % client.ready(namespace, name, expected_status='Ready'))
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        result = client.ready(namespace, name, expected_status='Ready', progress=progress)
    if result:
        ip_address = result['ip']
    else:
        raise ValueError('No IP address found for VirtualServer')

    # Add virtual server to SSH config so VSCode can open it automatically.
    with open(os.path.expanduser('~/.ssh/config'), 'r') as f:
        text = f.read()
    pattern = f'Host {name}\n  HostName [0-9.]+'
    if re.search(pattern, text):
        text = re.sub(pattern, f'Host {name}\n  HostName {ip_address}', text)
    else:
        text += f'\nHost {name}\n  HostName {ip_address}\n  User {username}\n'
    with open(os.path.expanduser('~/.ssh/config'), 'w') as f:
        f.write(text)

    # Run a ping test to check if the server is up.
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        transient=True,
    ) as progress:
        progress.add_task(description=f'Pinging {name} at {ip_address}...')
        while os.system(f'ping -c 1 {ip_address}') != 0:
            time.sleep(1)
        progress.add_task(description=f'{name} is up at {ip_address} :tada:')
    print(f'\n\n{name} is up at {ip_address} :tada:')
    print(f'You can now SSH into {name} with `ssh {username}@{name}`')
    print(f'You can alternatively open {name} by doing "Connect to Host" in VSCode.')

@app.command()
def stop(name: str, namespace: str):
    try:
        client.kubevirt_api.stop(namespace, name)
        print(f'VirtualServer {name} in namespace {namespace} stopped')
    except ApiException as e:
        print(f'Error stopping VirtualServer {name} in namespace {namespace}: {e}')


@app.command()
def start(name: str, namespace: str):
    try:
        client.kubevirt_api.start(namespace, name)
        print(f'VirtualServer {name} in namespace {namespace} started')
    except ApiException as e:
        print(f'Error starting VirtualServer {name} in namespace {namespace}: {e}')


@app.command()
def update(
    name: str, 
    namespace: str, 
    n_cpu: int = 32,
    n_gpu: int = 4,
    memory: str = '128Gi',
    disk_size: str = '800Gi',
):
    raise NotImplementedError('Update not implemented yet')
    # manifest = client.get(name, namespace) # TODO: does this work?
    # manifest['spec']['resources']['cpu']['count'] = n_cpu
    # manifest['spec']['resources']['gpu']['count'] = n_gpu
    # manifest['spec']['resources']['memory'] = memory
    # manifest['spec']['resources']['diskSize'] = disk_size
    # client.update(manifest)
    # print(f'VirtualServer {name} in namespace {namespace} updated')


@app.command()
def delete(name: str, namespace: str = os.environ['KUBERNETES_NAMESPACE']):
    client.delete(namespace, name)
    print(f':axe: [red]VirtualServer {name} in namespace {namespace} deleted[/red]')

if __name__ == '__main__':
    if 'KUBERNETES_NAMESPACE' not in os.environ:
        raise ValueError('KUBERNETES_NAMESPACE not set in environment.')
    app()