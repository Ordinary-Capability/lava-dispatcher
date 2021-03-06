# Copyright (C) 2014 Linaro Limited
#
# Author: Neil Williams <neil.williams@linaro.org>
#
# This file is part of LAVA Dispatcher.
#
# LAVA Dispatcher is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# LAVA Dispatcher is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along
# with this program; if not, see <http://www.gnu.org/licenses>.

import os
from lava_dispatcher.pipeline.action import (
    Pipeline,
    Action,
    JobError,
)
from lava_dispatcher.pipeline.logical import Boot, RetryAction
from lava_dispatcher.pipeline.actions.boot import BootAction
from lava_dispatcher.pipeline.actions.boot.environment import ExportDeviceEnvironment
from lava_dispatcher.pipeline.shell import (
    ExpectShellSession,
    ShellCommand,
    ShellSession
)
from lava_dispatcher.pipeline.utils.shell import which
from lava_dispatcher.pipeline.utils.strings import substitute
from lava_dispatcher.pipeline.actions.boot import AutoLoginAction


# FIXME: decide if 'media: tmpfs' is necessary or remove from YAML. Only removable needs 'media'
class BootQEMU(Boot):
    """
    The Boot method prepares the command to run on the dispatcher but this
    command needs to start a new connection and then allow AutoLogin, if
    enabled, and then expect a shell session which can be handed over to the
    test method. self.run_command is a blocking call, so Boot needs to use
    a direct spawn call via ShellCommand (which wraps pexpect.spawn) then
    hand this pexpect wrapper to subsequent actions as a shell connection.
    """

    compatibility = 4

    def __init__(self, parent, parameters):
        super(BootQEMU, self).__init__(parent)
        self.action = BootQEMUImageAction()
        self.action.section = self.action_type
        self.action.job = self.job
        parent.add_action(self.action, parameters)

    @classmethod
    def accepts(cls, device, parameters):
        if 'qemu' not in device['actions']['boot']['methods']:
            return False
        if 'method' not in parameters:
            return False
        if parameters['method'] not in ['qemu', 'monitor']:
            return False
        return True


class BootQEMUImageAction(BootAction):

    def __init__(self):
        super(BootQEMUImageAction, self).__init__()
        self.name = 'boot_image_retry'
        self.description = "boot image with retry"
        self.summary = "boot with retry"

    def populate(self, parameters):
        self.internal_pipeline = Pipeline(parent=self, job=self.job, parameters=parameters)
        self.internal_pipeline.add_action(BootQemuRetry())
        if self.has_prompts(parameters):
            self.internal_pipeline.add_action(AutoLoginAction())
            if self.test_has_shell(parameters):
                self.internal_pipeline.add_action(ExpectShellSession())
                self.internal_pipeline.add_action(ExportDeviceEnvironment())


class BootQemuRetry(RetryAction):

    def __init__(self):
        super(BootQemuRetry, self).__init__()
        self.name = 'boot_qemu_image'
        self.description = "boot image using QEMU command line"
        self.summary = "boot QEMU image"

    def populate(self, parameters):
        self.internal_pipeline = Pipeline(parent=self, job=self.job, parameters=parameters)
        self.internal_pipeline.add_action(CallQemuAction())


class CallQemuAction(Action):

    def __init__(self):
        super(CallQemuAction, self).__init__()
        self.name = "execute-qemu"
        self.description = "call qemu to boot the image"
        self.summary = "execute qemu to boot the image"
        self.sub_command = []

    def validate(self):
        super(CallQemuAction, self).validate()
        if self.parameters['method'] == 'qemu' and 'prompts' not in self.parameters:
            if self.test_has_shell(self.parameters):
                self.errors = "Unable to identify boot prompts from job definition."
        try:
            boot = self.job.device['actions']['boot']['methods']['qemu']
            if 'parameters' not in boot or 'command' not in boot['parameters']:
                self.errors = "Invalid device configuration - missing parameters"
            elif not boot['parameters']['command']:
                self.errors = "No QEMU binary command found - missing context."
            qemu_binary = which(boot['parameters']['command'])
            self.sub_command = [qemu_binary]
            self.sub_command.extend(boot['parameters'].get('options', []))
            self.sub_command.extend(
                ['%s' % item for item in boot['parameters'].get('extra', [])])
        except AttributeError as exc:
            self.errors = "Unable to parse device options: %s %s" % (
                exc, self.job.device['actions']['boot']['methods']['qemu'])
        except (KeyError, TypeError):
            self.errors = "Invalid parameters for %s" % self.name
        substitutions = {}
        commands = []
        namespace = self.parameters.get('namespace', 'common')
        for label in self.data[namespace]['download_action'].keys():
            if label == 'offset' or label == 'available_loops' or label == 'uefi':
                continue
            image_arg = self.get_namespace_data(action='download_action', label=label, key='image_arg')
            action_arg = self.get_namespace_data(action='download_action', label=label, key='file')
            if not image_arg or not action_arg:
                self.errors = "Missing image_arg for %s. " % label
                continue
            substitutions["{%s}" % label] = action_arg
            commands.append(image_arg)
        self.sub_command.extend(substitute(commands, substitutions))
        if not self.sub_command:
            self.errors = "No QEMU command to execute"
        uefi_dir = self.get_namespace_data(action='deployimages', label='image', key='uefi_dir')
        if uefi_dir:
            self.sub_command.extend(['-L', uefi_dir, '-monitor', 'none'])

    def run(self, connection, max_end_time, args=None):
        """
        CommandRunner expects a pexpect.spawn connection which is the return value
        of target.device.power_on executed by boot in the old dispatcher.

        In the new pipeline, the pexpect.spawn is a ShellCommand and the
        connection is a ShellSession. CommandRunner inside the ShellSession
        turns the ShellCommand into a runner which the ShellSession uses via ShellSession.run()
        to run commands issued *after* the device has booted.
        pexpect.spawn is one of the raw_connection objects for a Connection class.
        """
        # initialise the first Connection object, a command line shell into the running QEMU.
        guest = self.get_namespace_data(action='apply-overlay-guest', label='guest', key='filename')
        if guest:
            self.logger.info("Extending command line for qcow2 test overlay")
            # interface is ide by default in qemu
            interface = self.job.device['actions']['deploy']['methods']['image']['parameters']['guest'].get('interface', 'ide')
            self.sub_command.append('-drive format=qcow2,file=%s,media=disk,if=%s' %
                                    (os.path.realpath(guest), interface))
            # push the mount operation to the test shell pre-command to be run
            # before the test shell tries to execute.
            shell_precommand_list = []
            mountpoint = self.get_namespace_data(action='test', label='results', key='lava_test_results_dir')
            uuid = '/dev/disk/by-uuid/%s' % self.get_namespace_data(action='apply-overlay-guest', label='guest', key='UUID')
            shell_precommand_list.append('mkdir %s' % mountpoint)
            # prepare_guestfs always uses ext2
            shell_precommand_list.append('mount %s -t ext2 %s' % (uuid, mountpoint))
            # debug line to show the effect of the mount operation
            # also allows time for kernel messages from the mount operation to be processed.
            shell_precommand_list.append('ls -la %s/bin/lava-test-runner' % mountpoint)
            self.set_namespace_data(action='test', label='lava-test-shell', key='pre-command-list', value=shell_precommand_list)

        self.logger.info("Boot command: %s", ' '.join(self.sub_command))
        shell = ShellCommand(' '.join(self.sub_command), self.timeout, logger=self.logger)
        if shell.exitstatus:
            raise JobError("%s command exited %d: %s" % (self.sub_command, shell.exitstatus, shell.readlines()))
        self.logger.debug("started a shell command")

        shell_connection = ShellSession(self.job, shell)
        shell_connection = super(CallQemuAction, self).run(shell_connection, max_end_time, args)

        # FIXME: tests with multiple boots need to be handled too.
        res = 'failed' if self.errors else 'success'
        self.set_namespace_data(action='boot', label='shared', key='boot-result', value=res)
        self.set_namespace_data(action='shared', label='shared', key='connection', value=shell_connection)
        return shell_connection


# FIXME: implement a QEMU protocol to monitor VM boots
