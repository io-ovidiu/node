#!/usr/bin/env python3

from pathlib import Path
from collections import defaultdict
from time import time, sleep
import logging
import os
import base64
import json
import sys
from urllib.error import HTTPError
import colorlog
import click

from liquid_node.collections import push_collections_titles
from liquid_node.import_from_docker import validate_names, ensure_docker_setup_stopped, \
    add_collections_ini, import_index
from liquid_node.collections import get_collections_to_purge, purge_collection
from liquid_node.configuration import config
from liquid_node.consul import consul
from liquid_node.jobs import get_job, get_collection_job, hoover
from liquid_node.nomad import nomad
from liquid_node.process import run
from liquid_node.util import first
from liquid_node.collections import get_search_collections
from liquid_node.docker import docker
from liquid_node.vault import vault
from liquid_node.import_from_docker import import_collection


@click.group()
def liquid_commands():
    """ Manage liquid on a nomad cluster. """
    pass


log = logging.getLogger(__name__)

CORE_AUTH_APPS = [
    {
        'name': 'authdemo',
        'vault_path': 'liquid/authdemo/auth.oauth2',
        'callback': f'{config.app_url("authdemo")}/__auth/callback',
    },
    {
        'name': 'hoover',
        'vault_path': 'liquid/hoover/auth.oauth2',
        'callback': f'{config.app_url("hoover")}/__auth/callback',
    },
    {
        'name': 'dokuwiki',
        'vault_path': 'liquid/dokuwiki/auth.oauth2',
        'callback': f'{config.app_url("dokuwiki")}/__auth/callback',
    },
    {
        'name': 'rocketchat-authproxy',
        'vault_path': 'liquid/rocketchat/auth.oauth2',
        'callback': f'{config.app_url("rocketchat")}/__auth/callback',
    },
    {
        'name': 'rocketchat-app',
        'vault_path': 'liquid/rocketchat/app.oauth2',
        'callback': f'{config.app_url("rocketchat")}/_oauth/liquid',
    },
    {
        'name': 'nextcloud',
        'vault_path': 'liquid/nextcloud/auth.oauth2',
        'callback': f'{config.app_url("nextcloud")}/__auth/callback',
    },
]


def random_secret(bits=256):
    """ Generate a crypto-quality 256-bit random string. """
    return str(base64.b16encode(os.urandom(int(bits / 8))), 'latin1').lower()


def ensure_secret(path, get_value):
    if not vault.read(path):
        log.info(f"Generating value for {path}")
        vault.set(path, get_value())


def ensure_secret_key(path):
    ensure_secret(path, lambda: {'secret_key': random_secret()})


def wait_for_service_health_checks(health_checks):
    """Waits health checks to become green for green_count times in a row. """

    def get_checks():
        """Generates a list of (service, check, status)
        for all failing checks after checking with Consul"""

        consul_status = {}
        for service in health_checks:
            for s in consul.get(f'/health/checks/{service}'):
                key = service, s['Name']
                if key in consul_status:
                    consul_status[key] = 'appears twice. Maybe halt, restart Consul and try again?'
                    continue
                consul_status[key] = s['Status']

        for service, checks in health_checks.items():
            for check in checks:
                status = consul_status.get((service, check), 'missing')
                yield service, check, status

    t0 = time()
    last_check_timestamps = {}
    passing_count = defaultdict(int)

    def log_checks(checks, as_error=False):
        max_service_len = max(len(s) for s in health_checks.keys())
        max_name_len = max(max(len(name) for name in health_checks[key]) for key in health_checks)
        now = time()
        for service, check, status in checks:
            last_time = last_check_timestamps.get((service, check), t0)
            after = f'{now - last_time:+.1f}s'
            last_check_timestamps[service, check] = now

            line = f'[{time() - t0:4.1f}] {service:>{max_service_len}}: {check:<{max_name_len}} {status.upper():<8} {after:>5}'  # noqa: E501

            if status == 'passing':
                if as_error:
                    continue
                passing_count[service, check] += 1
                if passing_count[service, check] > 1:
                    line += f' #{passing_count[service, check]}'
                log.info(line)
            elif as_error:
                log.error(line)
            else:
                log.warning(line)

    services = sorted(health_checks.keys())
    log.info(f"Waiting for health checks on {services}")

    greens = 0
    timeout = t0 + config.wait_max + config.wait_interval * config.wait_green_count
    last_checks = set(get_checks())
    log_checks(last_checks)
    while time() < timeout:
        sleep(config.wait_interval)

        checks = set(get_checks())
        log_checks(checks - last_checks)
        last_checks = checks

        if any(status != 'passing' for _, _, status in checks):
            greens = 0
        else:
            greens += 1

        if greens >= config.wait_green_count:
            log.info(f"Checks green {services} after {time() - t0:.02f}s")
            return

        # No chance to get enough greens
        no_chance_timestamp = timeout - config.wait_interval * config.wait_green_count
        if greens == 0 and time() >= no_chance_timestamp:
            break

    log_checks(checks, as_error=True)
    msg = f'Checks are failed after {time() - t0:.02f}s.'
    raise RuntimeError(msg)


@liquid_commands.command()
def resources():
    """Get memory and CPU usage for the deployment"""

    def get_all_res():
        jobs = [nomad.parse(get_job(job.template)) for job in config.jobs]
        for name, settings in config.collections.items():
            job = get_collection_job(name, settings, 'collection.nomad')
            jobs.append(nomad.parse(job))
            deps_job = get_collection_job(name, settings, 'collection-deps.nomad')
            jobs.append(nomad.parse(deps_job))
        for spec in jobs:
            yield from nomad.get_resources(spec)

    total = defaultdict(int)
    for name, _type, res in get_all_res():
        for key in ['MemoryMB', 'CPU', 'EphemeralDiskMB']:
            if key not in res:
                continue
            if res[key] is None:
                raise RuntimeError("Please update Nomad to 0.9.3+")
            total[f'{_type} {key}'] += res[key]

    print('Resource requirement totals: ')
    for key, value in sorted(total.items()):
        print(f'  {key}: {value}')


def check_system_config():
    """Raises errors if the system is improperly configured.

    This checks if elasticsearch will accept our
    vm.max_map_count kernel parameter value.
    """

    assert int(run("sysctl -n vm.max_map_count")) >= 262144, \
        'the "vm.max_map_count" kernel parameter is too low, check readme'


@liquid_commands.command()
def deploy():
    """Run all the jobs in nomad."""

    check_system_config()

    consul.set_kv('liquid_domain', config.liquid_domain)
    consul.set_kv('liquid_debug', 'true' if config.liquid_debug else 'false')
    consul.set_kv('liquid_http_protocol', config.liquid_http_protocol)

    vault.ensure_engine()

    vault_secret_keys = [
        'liquid/liquid/core.django',
        'liquid/hoover/auth.django',
        'liquid/hoover/search.django',
        'liquid/hoover/search.postgres',
        'liquid/authdemo/auth.django',
        'liquid/nextcloud/nextcloud.admin',
        'liquid/nextcloud/nextcloud.uploads',
        'liquid/nextcloud/nextcloud.maria',
        'liquid/dokuwiki/auth.django',
        'liquid/nextcloud/auth.django',
        'liquid/rocketchat/auth.django',
        'liquid/ci/vmck.django',
        'liquid/ci/drone.secret',
    ]
    core_auth_apps = list(CORE_AUTH_APPS)

    for job in config.jobs:
        vault_secret_keys += list(job.vault_secret_keys)
        core_auth_apps += list(job.core_auth_apps)

    for path in vault_secret_keys:
        ensure_secret_key(path)

    if config.ci_enabled:
        vault.set('liquid/ci/drone.github', {
            'client_id': config.ci_github_client_id,
            'client_secret': config.ci_github_client_secret,
            'user_filter': config.ci_github_user_filter,
        })
        vault.set('liquid/ci/drone.docker', {
            'username': config.ci_docker_username,
            'password': config.ci_docker_password,
        })

    def start(job, hcl):
        log.info('Starting %s...', job)
        spec = nomad.parse(hcl)
        nomad.run(spec)
        job_checks = {}
        for service, checks in nomad.get_health_checks(spec):
            if not checks:
                log.warn(f'service {service} has no health checks')
                continue
            job_checks[service] = checks
        return job_checks

    jobs = [(job.name, get_job(job.template)) for job in config.jobs]

    hov_deps = hoover.Deps()
    database_tasks = [hov_deps.pg_task]
    deps_jobs = [(hov_deps.name, get_job(hov_deps.template))]
    for name, settings in config.collections.items():
        job = get_collection_job(name, settings)
        jobs.append((f'collection-{name}', job))
        deps_job = get_collection_job(name, settings, 'collection-deps.nomad')
        deps_jobs.append((f'collection-{name}-deps', deps_job))
        database_tasks.append('snoop-' + name + '-pg')
        ensure_secret_key(f'liquid/collections/{name}/snoop.django')
        ensure_secret_key(f'liquid/collections/{name}/snoop.postgres')

    ensure_secret('liquid/rocketchat/adminuser', lambda: {
        'username': 'rocketchatadmin',
        'pass': random_secret(64),
    })

    # Start liquid-core in order to setup the auth
    liquid_checks = start('liquid', dict(jobs)['liquid'])
    wait_for_service_health_checks({'core': liquid_checks['core']})

    for app in core_auth_apps:
        log.info('Auth %s -> %s', app['name'], app['callback'])
        cmd = ['./manage.py', 'createoauth2app', app['name'], app['callback']]
        containers = docker.containers([('liquid_task', 'liquid-core')])
        container_id = first(containers, 'liquid-core containers')
        docker_exec_cmd = ['docker', 'exec', container_id] + cmd
        tokens = json.loads(run(docker_exec_cmd, shell=False))
        vault.set(app['vault_path'], tokens)

    # only start deps jobs + hoover
    health_checks = {}
    for job, hcl in deps_jobs:
        job_checks = start(job, hcl)
        health_checks.update(job_checks)

    # wait for database health checks
    pg_checks = {k: v for k, v in health_checks.items() if k in database_tasks}
    wait_for_service_health_checks(pg_checks)

    # run the set password script
    for collection in sorted(config.collections.keys()):
        docker.exec_(f'snoop-{collection}-pg', 'sh', '/local/set_pg_password.sh')
    docker.exec_(f'hoover-pg', 'sh', '/local/set_pg_password.sh')

    # wait until all deps are healthy
    wait_for_service_health_checks(health_checks)

    for job, hcl in jobs:
        job_checks = start(job, hcl)
        health_checks.update(job_checks)

    # Wait for everything else
    wait_for_service_health_checks(health_checks)

    # Run initcollection for all unregistered collections
    already_initialized = sorted(get_search_collections())
    for collection in sorted(config.collections.keys()):
        if collection not in already_initialized:
            log.info('Initializing collection: %s', collection)
            initcollection(collection)
        else:
            log.info('Already initialized collection: %s', collection)

    push_collections_titles()
    log.info("Deploy done!")


@liquid_commands.command()
def halt():
    """Stop all the jobs in nomad."""

    jobs = [j.name for j in config.jobs]
    jobs.extend(f'collection-{name}' for name in config.collections)
    jobs.extend(f'collection-{name}-deps' for name in config.collections)
    for job in jobs:
        log.info('Stopping %s...', job)
        nomad.stop(job)


@liquid_commands.command()
def collectionsgc():
    """Stop collections jobs that are no longer declared in the ini file."""

    stopped_jobs = []
    for job in nomad.jobs():
        if job['ID'].startswith('collection-'):
            collection_name = job['ID'][len('collection-'):]
            if collection_name not in config.collections and job['Status'] == 'running':
                log.info('Stopping %s...', job['ID'])
                nomad.stop(job['ID'])
                stopped_jobs.append(job['ID'])

    log.info(f'Waiting for jobs to die...')
    timeout = time() + config.wait_max
    while stopped_jobs and time() < timeout:
        sleep(config.wait_interval)

        nomad_jobs = {job['ID']: job for job in nomad.jobs() if job['ID'] in stopped_jobs}
        for job_name in stopped_jobs:
            if job_name not in nomad_jobs or nomad_jobs[job_name]['Status'] == 'dead':
                stopped_jobs.remove(job_name)
                log.info(f'Job {job_name} is dead')
    if stopped_jobs:
        raise RuntimeError(f'The following jobs are still running: {stopped_jobs}')


@liquid_commands.command()
def nomadgc():
    """Remove dead jobs from nomad"""
    nomad.gc()


@liquid_commands.command()
def nomad_address():
    """Print the nomad address."""

    print(nomad.get_address())


@liquid_commands.command()
@click.argument('job')
@click.argument('group')
def alloc(job, group):
    """Print the ID of the current allocation of the job and group.

    :param job: the job identifier
    :param group: the group identifier
    """

    allocs = nomad.job_allocations(job)
    running = [
        a['ID'] for a in allocs
        if a['ClientStatus'] == 'running' and a['TaskGroup'] == group
    ]
    print(first(running, 'running allocations'))


@liquid_commands.command()
@click.argument('name')
def initcollection(name):
    """Initialize collection with given name.

    Create the snoop database, create the search index, run dispatcher, add collection
    to search.

    :param name: the collection name
    """

    if name not in config.collections:
        raise RuntimeError('Collection %s does not exist in the liquid.ini file.', name)

    if name in get_search_collections():
        log.warning(f'Collection "{name}" was already initialized.')
        return

    docker.exec_(f'snoop-{name}-api', './manage.py', 'initcollection')

    docker.exec_(
        'hoover-search',
        './manage.py', 'addcollection', name,
        '--index', name,
        f'http://{nomad.get_address()}:8765/{name}/collection/json',
        '--public',
    )


@liquid_commands.command()
@click.option('--force', is_flag=True)
def purge(force=False):
    """Purge collections no longer declared in the ini file

    Remove the residual data and the hoover search index for collections that are no
    longer declared in the ini file.
    """

    to_purge = get_collections_to_purge()
    if not to_purge:
        print('No collections to purge.')
        return

    if to_purge:
        print('The following collections will be purged:')
        for coll in to_purge:
            print(' - ', coll)
        print('')

    if not force:
        confirm = None
        while confirm not in ['y', 'n']:
            print('Please confirm collections purge [y/n]: ', end='')
            confirm = input().lower()
            if confirm not in ['y', 'n']:
                print(f'Invalid input: {confirm}')

    if force or confirm == 'y':
        for coll in to_purge:
            print(f'Purging collection {coll}...')
            purge_collection(coll)
    else:
        print('No collections will be purged')


@liquid_commands.command()
@click.argument('name')
def deletecollection(name):
    """Delete a collection by name"""
    nomad.stop(f'collection-{name}')
    purge_collection(name)


@liquid_commands.command()
@click.argument('path')
@click.argument('method', required=False)
def importfromdockersetup(path, method='link'):
    """Import collections from existing docker-setup deployment.

    :param path: path to the docker-setup deployment
    :param move: if true, move data from the docker-setup deployment, otherwise copy data
    """
    docker_setup = Path(path).resolve()

    docker_compose_file = docker_setup / 'docker-compose.yml'
    if not docker_compose_file.is_file():
        raise RuntimeError(f'Path {docker_setup} is not a docker-setup deployment.')

    collections_json = docker_setup / 'settings' / 'collections.json'
    if not collections_json.is_file():
        log.info(f'Unable to find any collections in {docker_setup}.')
        return

    if config.collections:
        raise RuntimeError('Please remove existing collections before importing.')
    if get_collections_to_purge():
        raise RuntimeError('Please purge existing collections before importing')

    with open(str(collections_json)) as collections_file:
        collections = json.load(collections_file)
    validate_names(collections)

    ensure_docker_setup_stopped()
    halt()

    for name, settings in collections.items():
        import_collection(name, settings, docker_setup, method)
    import_index(docker_setup, method)

    add_collections_ini(collections)
    print()
    print('After adding the lines, re-run "./liquid deploy"')


@liquid_commands.command()
@click.argument('name')
@click.argument('args', nargs=-1)
def shell(name, *args):
    """Open a shell in a docker container tagged with liquid_task=`name`"""

    docker.shell(name, *args)


@liquid_commands.command()
@click.argument('name')
@click.argument('args', nargs=-1)
def dockerexec(name, *args):
    """Run `docker exec` in a container tagged with liquid_task=`name`"""

    docker.exec_(name, *args)


@liquid_commands.command()
@click.argument('path', required=False)
def getsecret(path=None):
    """Get a Vault secret"""

    if path:
        print(vault.read(path))

    else:
        for section in vault.list():
            for key in vault.list(section):
                print(f'{section}{key}')


if __name__ == '__main__':
    from liquid_node.configuration import config
    level = logging.DEBUG if config.liquid_debug else logging.INFO
    handler = colorlog.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(colorlog.ColoredFormatter(
        '%(asctime)s %(log_color)s%(levelname)8s %(message)s'))
    logging.basicConfig(
        handlers=[handler],
        level=level,
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    try:
        liquid_commands()
    except HTTPError as e:
        log.exception("HTTP Error %r: %r", e, e.file.read())
        sys.exit(1)
