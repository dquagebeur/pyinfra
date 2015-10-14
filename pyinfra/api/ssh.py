# pyinfra
# File: pyinfra/api/ssh.py
# Desc: handle all SSH related stuff

from os import path
from hashlib import sha1
from socket import (
    gaierror,
    error as socket_error, timeout as timeout_error
)

import gevent
from termcolor import colored
from paramiko import (
    SSHClient, SFTPClient, RSAKey,
    MissingHostKeyPolicy, SSHException, AuthenticationException
)

from pyinfra import state, logger
from pyinfra.api.util import read_buffer


def _connect(hostname, **kwargs):
    '''Connect to a single host. Returns the hostname if succesful.'''
    try:
        # Create new client & connect to the host
        client = SSHClient()
        client.set_missing_host_key_policy(MissingHostKeyPolicy())
        client.connect(hostname, **kwargs)

        # Log
        logger.info(u'[{0}] {1}'.format(
            colored(hostname, attrs=['bold']),
            colored('Connected', 'green')
        ))

        return (hostname, client)

    except AuthenticationException as e:
        logger.critical(u'Auth error on: {0}, {1}'.format(hostname, e))
    except SSHException as e:
        logger.critical(u'SSH error on: {0}, {1}'.format(hostname, e))
    except socket_error as e:
        logger.critical(u'Could not connect: {0}, {1}'.format(hostname, e))
    except gaierror:
        logger.critical(u'Could not resolve: {0}'.format(hostname))


def connect_all():
    '''Connect to all the configured servers in parallel. Reads/writes state.inventory.'''
    greenlets = []

    for host in state.inventory:
        kwargs = {
            'username': host.data.ssh_user,
            'port': host.data.ssh_port,
            'timeout': state.config.TIMEOUT
        }

        # Password auth (boo!)
        if host.data.ssh_password:
            kwargs['password'] = host.data.ssh_password

        # Key auth!
        elif host.data.ssh_key:
            ssh_key_filenames = [
                # Global from executed directory
                path.expanduser(host.data.ssh_key),
                # Relative to the deploy
                path.join(state.deploy_dir, host.data.ssh_key)
            ]

            for filename in ssh_key_filenames:
                if path.isfile(filename):
                    kwargs['pkey'] = RSAKey.from_private_key_file(
                        filename=filename,
                        password=host.data.ssh_key_password
                    )
                    break

        greenlets.append(
            state.pool.spawn(_connect, host.ssh_hostname, **kwargs)
        )

    gevent.wait(greenlets)

    # Get/set the results
    results = [greenlet.get() for greenlet in greenlets]
    hostname_clients = {
        result[0]: result[1]
        for result in results
        if result
    }

    for hostname, client in hostname_clients.iteritems():
        state.ssh_connections[hostname] = client

    state.inventory.connected_hosts = set(filter(None, hostname_clients.keys()))


def run_shell_command(
    hostname, command,
    sudo=False, sudo_user=None, env=None, timeout=None, print_output=False, print_prefix=''
):
    '''Execute a command on the specified host.'''
    if env is None:
        env = {}

    logger.debug(u'Running command on {0}: "{1}"'.format(hostname, command))
    logger.debug(u'Command sudo?: {0}, sudo user: {1}, env: {2}'.format(
        sudo, sudo_user, env
    ))

    # Use env & build our actual command
    if env:
        env_string = ' '.join([
            '{0}={1}'.format(key, value)
            for key, value in env.iteritems()
        ])
        command = '{0} {1}'.format(env_string, command)

    # Escape "'s
    command = command.replace("'", "\\'")

    # No sudo, just sh wrap the command
    if not sudo:
        command = "sh -c '{0}'".format(command)
    # Otherwise, work out sudo
    else:
        # Sudo with a user, then sh
        if sudo_user:
            command = "sudo -H -u {0} -S sh -c '{1}'".format(sudo_user, command)
        # Sudo then sh
        else:
            command = "sudo -H -S sh -c '{0}'".format(command)

    if print_output:
        print '{0}>>> {1}'.format(print_prefix, command)

    # Get the connection for this hostname
    connection = state.ssh_connections[hostname]

    # Run it! Get stdout, stderr & the underlying channel
    _, stdout_buffer, stderr_buffer = connection.exec_command(command)
    channel = stdout_buffer.channel

    # Iterate through outputs to get an exit status and generate desired list output,
    # done in two greenlets so stdout isn't printed before stderr. Not attached to
    # state.*_pool to avoid blocking it with 2x n-hosts greenlets.
    stdout_reader = gevent.spawn(
        read_buffer, stdout_buffer,
        print_output=print_output,
        print_func=lambda line: u'{0}{1}'.format(print_prefix, line)
    )
    stderr_reader = gevent.spawn(
        read_buffer, stderr_buffer,
        print_output=print_output,
        print_func=lambda line: u'{0}{1}'.format(print_prefix, colored(line, 'red'))
    )

    # Wait on output, with our timeout
    greenlets = gevent.wait((stdout_reader, stderr_reader), timeout=timeout)

    # Timeout doesn't raise an exception, but gevent.wait returns the greenlets which did
    # complete. So if both haven't completed, we kill them and fail with a timeout.
    if len(greenlets) != 2:
        stdout_reader.kill()
        stderr_reader.kill()
        raise timeout_error()

    stdout = stdout_reader.get()
    stderr = stderr_reader.get()
    return channel, stdout, stderr


def _get_sftp_connection(hostname):
    # SFTP connections aren't *required* for deploys, so we create them on-demand
    if hostname in state.sftp_connections:
        return state.sftp_connections[hostname]

    ssh_connection = state.ssh_connections[hostname]
    transport = ssh_connection.get_transport()
    client = SFTPClient.from_transport(transport)

    state.sftp_connections[hostname] = client

    return client

def _put_file(hostname, file_io, remote_location):
    sftp = _get_sftp_connection(hostname)
    sftp.putfo(file_io, remote_location)

def put_file(
    hostname, file_io, remote_file,
    sudo=False, sudo_user=None, print_output=False, print_prefix=''
):
    '''Upload file-ios to the specified host.'''
    if not sudo:
        _put_file(hostname, file_io, remote_file)
    else:
        # sudo is a little more complicated, as you can only sftp with the SSH user connected
        # so upload to tmp and copy/chown w/sudo

        # Get temp file location
        hash_ = sha1()
        hash_.update(remote_file)
        temp_file = '/tmp/{0}'.format(hash_.hexdigest())

        _put_file(hostname, file_io, temp_file)

        # Execute run_shell_command w/sudo to mv/chown it
        command = 'mv {0} {1}'.format(temp_file, remote_file)
        if sudo_user:
            command = '{0} && chown {1} {2}'.format(command, sudo_user, remote_file)

        channel, _, stderr = run_shell_command(
            hostname, command,
            sudo=sudo, sudo_user=sudo_user,
            print_output=print_output,
            print_prefix=print_prefix
        )

        if channel.exit_status > 0:
            logger.critical(u'File error: {0}'.format('\n'.join(stderr)))
            return False

    if print_output:
        print u'{0}file uploaded: {1}'.format(print_prefix, remote_file)
