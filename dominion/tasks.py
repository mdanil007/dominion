# Copyright 2016 Evgeny Golyshev. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import errno
import os
import pty
import shutil
import signal
import socket
import subprocess
import threading
import time

import django
from celery import Celery, bootsteps
from celery.bin import Option
from celery.utils.log import get_task_logger

import dominion.util
from firmwares.models import Firmware
from users.models import User


app = Celery('tasks', backend='rpc://', broker='amqp://guest@localhost//')
app.user_options['worker'].add(Option(
    '--base-system',
    dest='base_system',
    default='/var/dominion/jessie-armhf',
    help='The path to a chroot environment which contains '
         'the Debian base system')
)
app.user_options['worker'].add(Option(
    '--builder-location',
    dest='builder_location',
    default='/var/dominion/rpi2-gen-image',
    help='')
)
app.user_options['worker'].add(Option(
    '--workspace',
    dest='workspace',
    help='')
)

django.setup()


class ConfigBootstep(bootsteps.Step):
    def __init__(self, worker,
                 base_system=None, builder_location=None, workspace=None,
                 **options):
        if base_system:
            # TODO: check if the specified directory exists
            app.conf['BASE_SYSTEM'] = base_system

        if builder_location:
            app.conf['BUILDER_LOCATION'] = builder_location

        if workspace:
            app.conf['WORKSPACE'] = workspace
        else:
            app.conf['WORKSPACE'] = '/tmp/dominion'
            if not os.path.exists(app.conf['WORKSPACE']):
                os.makedirs(app.conf['WORKSPACE'])

app.steps['worker'].add(ConfigBootstep)

LOGGER = get_task_logger(__name__)
MAGIC_PHRASE = b"Let's wind up"


def _pass_fd(sock, socket_name, fd):
    """Connects to the server when it's ready and passes fd to it"""

    while True:
        try:
            sock.connect(socket_name)
        except OSError as e:
            if e.errno == errno.ENOENT:
                LOGGER.debug('The socket does not exist')

            if e.errno == errno.ECONNREFUSED:
                LOGGER.debug('Connection refused')

            time.sleep(1)
            continue

        break

    dominion.util.send_fds(sock, fd)


def _get_user(user_id):
    try:
        return User.objects.get(id=user_id)
    except User.DoesNotExist:
        LOGGER.critical('User {} does not exist'.format(user_id))
        return None


def _send_email_notification(user_id, subject, message):
    user = _get_user(user_id)
    if user:
        if user.userprofile.email_notifications:
            user.email_user(subject, message)


@app.task(name='tasks.build')
def build(user_id, image):
    build_id = image['id']
    packages_list = image['selected_packages']
    if packages_list is None:
        packages_list = []
    root_password = image['root_password']
    users = image['users']
    target = image['target']
    configuration = image['configuration']
    base_system = app.conf.get('BASE_SYSTEM', './jessie-armhf')
    builder_location = app.conf.get('BUILDER_LOCATION', './rpi2-gen-image')
    workspace = app.conf.get('WORKSPACE')
    subject = ''
    message = ''

    # rpi23-gen-image creates
    # ./workspace/xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx/build, but we have to
    # create ./workspace/xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx/intermediate to
    # store the rootfs of the future image.
    target_dir = '{}/{}'.format(workspace, build_id)
    intermediate_dir = '{}/{}'.format(target_dir, 'intermediate')
    LOGGER.info('intermediate: {}'.format(intermediate_dir))

    pid, fd = pty.fork()
    if pid == 0:  # child
        # The chroot environment might not be ready yet, so the process sends
        # STOP to itself. Its parent will resume it later.
        os.kill(os.getpid(), signal.SIGSTOP)

        apt_includes = ','.join(packages_list) if packages_list else ''
        env = {
            'PATH': os.environ['PATH'],
            'BASEDIR': target_dir,
            'CHROOT_SOURCE': intermediate_dir,
            'IMAGE_NAME': target_dir,
            'WORKSPACE_DIR': workspace,
            'BUILD_ID': build_id,
            'RPI2_BUILDER_LOCATION': builder_location,
            'APT_INCLUDES': apt_includes
        }

        if root_password:
            env['ENABLE_ROOT'] = 'true'
            env['PASSWORD'] = root_password

        if target:
            model = '3' if target['device'] == 'Raspberry Pi 3' else '2'
            env['RPI_MODEL'] = model

        if users:
            user = users[0]  # rpi23-gen-image can't work with multiple users
            env['ENABLE_USER'] = 'true'
            env['USER_NAME'] = user['username']
            env['USER_PASSWORD'] = user['password']

        if configuration:
            allowed = [
                'HOSTNAME',
                'DEFLOCAL',
                'TIMEZONE',
                'ENABLE_REDUCE',
                'REDUCE_APT',
                'REDUCE_DOC',
                'REDUCE_MAN',
                'REDUCE_VIM',
                'REDUCE_BASH',
                'REDUCE_HWDB',
                'REDUCE_SSHD',
                'REDUCE_LOCALE'
            ]
            configuration = \
                {k: v for k, v in configuration.items() if k in allowed}
            env.update(configuration)

        command_line = ['sh', 'run.sh']
        os.execvpe(command_line[0], command_line, env)
    else:  # parent
        socket_name = '/tmp/' + build_id

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        thread = threading.Thread(target=_pass_fd,
                                  args=(sock, socket_name, fd))
        thread.start()

        try:
            os.makedirs(target_dir)
        except OSError:
            os.kill(pid, signal.SIGKILL)
            LOGGER.critical('The directory {} already exists'.
                            format(target_dir))

        command_line = ['cp', '-r', base_system, intermediate_dir]
        proc = subprocess.Popen(command_line)
        if proc.wait() != 0:
            os.kill(pid, signal.SIGKILL)
            LOGGER.critical('Cannot copy {} to {}'.
                            format(base_system, intermediate_dir))

        os.write(fd, b'Start building...\n')

        os.kill(pid, signal.SIGCONT)  # resume child process

        _, retcode = os.waitpid(pid, 0)
        shutil.rmtree(target_dir)  # cleaning up
        os.write(fd, MAGIC_PHRASE)

        if retcode == 0:
            user = _get_user(user_id)
            if user:
                firmware = Firmware(name=build_id, user=user)
                firmware.save()
                subject = '{} has built!'.format(image['target']['distro'])
                message = ('You can directly download it from Dashboard: '
                           'https://cusdeb.com/dashboard/')
        else:
            LOGGER.critical('Build failed: {}'.format(build_id))
            os.write(fd, b'Build process failed\n')
            subject = '{} build has failed!'.format(image['target']['distro'])
            message = ('Sorry, something went wrong. Cusdeb team has been '
                       'informed about the situation.')

        if subject and message:
            _send_email_notification(user_id, subject, message)

        return retcode
