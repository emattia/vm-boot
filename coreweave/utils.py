import yaml
from typing import Dict
import os


def from_yaml(filepath):
    with open(filepath, 'r') as file:
        return yaml.safe_load(file)


def fetch_ssh_public_key(filepath=os.path.expanduser('~/.ssh/id_rsa.pub')):
    with open(filepath, 'r') as file:
        return file.read().strip()
