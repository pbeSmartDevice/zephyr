# Copyright (c) 2018 Foundries.io
#
# SPDX-License-Identifier: Apache-2.0

import abc
import argparse
import os
import pathlib
import pickle
import shutil
import subprocess
import sys

from west import log
from west.util import quote_sh_list

from build_helpers import find_build_dir, is_zephyr_build, \
    FIND_BUILD_DIR_DESCRIPTION
from runners.core import BuildConfiguration
from zcmake import CMakeCache
from zephyr_ext_common import Forceable, cached_runner_config, \
    ZEPHYR_SCRIPTS

# This is needed to load edt.pickle files.
sys.path.append(str(ZEPHYR_SCRIPTS / 'dts'))

SIGN_DESCRIPTION = '''\
This command automates some of the drudgery of creating signed Zephyr
binaries for chain-loading by a bootloader.

In the simplest usage, run this from your build directory:

   west sign -t your_tool -- ARGS_FOR_YOUR_TOOL

Assuming your binary was properly built for processing and handling by
tool "your_tool", this creates zephyr.signed.bin and zephyr.signed.hex
files (if supported by "your_tool") which are ready for use by your
bootloader. The "ARGS_FOR_YOUR_TOOL" value can be any additional
arguments you want to pass to the tool, such as the location of a
signing key, a version identifier, etc.

See tool-specific help below for details.'''

SIGN_EPILOG = '''\
imgtool
-------

Currently, MCUboot's 'imgtool' tool is supported. To build a signed
binary you can load with MCUboot using imgtool, run this from your
build directory:

   west sign -t imgtool -- --key YOUR_SIGNING_KEY.pem

For this to work, either imgtool must be installed (e.g. using pip3),
or you must pass the path to imgtool.py using the -p option.

The image header size, alignment, and slot sizes are determined from
the build directory using .config and the device tree. A default
version number of 0.0.0+0 is used (which can be overridden by passing
"--version x.y.z+w" after "--key"). As shown above, extra arguments
after a '--' are passed to imgtool directly.'''


class ToggleAction(argparse.Action):

    def __call__(self, parser, args, ignored, option):
        setattr(args, self.dest, not option.startswith('--no-'))


class Sign(Forceable):
    def __init__(self):
        super(Sign, self).__init__(
            'sign',
            # Keep this in sync with the string in west-commands.yml.
            'sign a Zephyr binary for bootloader chain-loading',
            SIGN_DESCRIPTION,
            accepts_unknown_args=False)

    def do_add_parser(self, parser_adder):
        parser = parser_adder.add_parser(
            self.name,
            epilog=SIGN_EPILOG,
            help=self.help,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            description=self.description)

        parser.add_argument('-d', '--build-dir',
                            help=FIND_BUILD_DIR_DESCRIPTION)
        self.add_force_arg(parser)

        # general options
        group = parser.add_argument_group('tool control options')
        group.add_argument('-t', '--tool', choices=['imgtool', 'rimage'],
                           required=True,
                           help='''image signing tool name; imgtool and rimage
                           are currently supported''')
        group.add_argument('-p', '--tool-path', default=None,
                           help='''path to the tool itself, if needed''')
        group.add_argument('tool_args', nargs='*', metavar='tool_opt',
                           help='extra option(s) to pass to the signing tool')

        # bin file options
        group = parser.add_argument_group('binary (.bin) file options')
        group.add_argument('--bin', '--no-bin', dest='gen_bin', nargs=0,
                           action=ToggleAction,
                           help='''produce a signed .bin file?
                           (default: yes, if supported and unsigned bin
                           exists)''')
        group.add_argument('-B', '--sbin', metavar='BIN',
                           help='''signed .bin file name
                           (default: zephyr.signed.bin in the build
                           directory, next to zephyr.bin)''')

        # hex file options
        group = parser.add_argument_group('Intel HEX (.hex) file options')
        group.add_argument('--hex', '--no-hex', dest='gen_hex', nargs=0,
                           action=ToggleAction,
                           help='''produce a signed .hex file?
                           (default: yes, if supported and unsigned hex
                           exists)''')
        group.add_argument('-H', '--shex', metavar='HEX',
                           help='''signed .hex file name
                           (default: zephyr.signed.hex in the build
                           directory, next to zephyr.hex)''')

        return parser

    def do_run(self, args, ignored):
        self.args = args        # for check_force

        # Find the build directory and parse .config and DT.
        build_dir = find_build_dir(args.build_dir)
        self.check_force(os.path.isdir(build_dir),
                         'no such build directory {}'.format(build_dir))
        self.check_force(is_zephyr_build(build_dir),
                         "build directory {} doesn't look like a Zephyr build "
                         'directory'.format(build_dir))
        bcfg = BuildConfiguration(build_dir)

        # Decide on output formats.
        formats = []
        bin_exists = 'CONFIG_BUILD_OUTPUT_BIN' in bcfg
        if args.gen_bin:
            self.check_force(bin_exists,
                             '--bin given but CONFIG_BUILD_OUTPUT_BIN not set '
                             "in build directory's ({}) .config".
                             format(build_dir))
            formats.append('bin')
        elif args.gen_bin is None and bin_exists:
            formats.append('bin')

        hex_exists = 'CONFIG_BUILD_OUTPUT_HEX' in bcfg
        if args.gen_hex:
            self.check_force(hex_exists,

                             '--hex given but CONFIG_BUILD_OUTPUT_HEX not set '
                             "in build directory's ({}) .config".
                             format(build_dir))
            formats.append('hex')
        elif args.gen_hex is None and hex_exists:
            formats.append('hex')

        if not formats:
            log.dbg('nothing to do: no output files')
            return

        # Delegate to the signer.
        if args.tool == 'imgtool':
            signer = ImgtoolSigner()
        elif args.tool == 'rimage':
            signer = RimageSigner()
        # (Add support for other signers here in elif blocks)
        else:
            raise RuntimeError("can't happen")

        signer.sign(self, build_dir, bcfg, formats)


class Signer(abc.ABC):
    '''Common abstract superclass for signers.

    To add support for a new tool, subclass this and add support for
    it in the Sign.do_run() method.'''

    @abc.abstractmethod
    def sign(self, command, build_dir, bcfg, formats):
        '''Abstract method to perform a signature; subclasses must implement.

        :param command: the Sign instance
        :param build_dir: the build directory
        :param bcfg: BuildConfiguration for build directory
        :param formats: list of formats to generate ('bin', 'hex')
        '''


class ImgtoolSigner(Signer):

    def sign(self, command, build_dir, bcfg, formats):
        if not formats:
            return

        args = command.args
        b = pathlib.Path(build_dir)
        cache = CMakeCache.from_build_dir(build_dir)

        tool_path = self.find_imgtool(command, args)
        # The vector table offset is set in Kconfig:
        vtoff = self.get_cfg(command, bcfg, 'CONFIG_ROM_START_OFFSET')
        # Flash device write alignment and the partition's slot size
        # come from devicetree:
        flash = self.edt_flash_node(b)
        align, addr, size = self.edt_flash_params(flash)

        runner_config = cached_runner_config(build_dir, cache)
        if 'bin' in formats:
            in_bin = runner_config.bin_file
            if not in_bin:
                log.die("can't find unsigned .bin to sign")
        else:
            in_bin = None
        if 'hex' in formats:
            in_hex = runner_config.hex_file
            if not in_hex:
                log.die("can't find unsigned .hex to sign")
        else:
            in_hex = None

        log.banner('image configuration:')
        log.inf('partition offset: {0} (0x{0:x})'.format(addr))
        log.inf('partition size: {0} (0x{0:x})'.format(size))
        log.inf('rom start offset: {0} (0x{0:x})'.format(vtoff))

        # Base sign command.
        #
        # We provide a default --version in case the user is just
        # messing around and doesn't want to set one. It will be
        # overridden if there is a --version in args.tool_args.
        sign_base = [tool_path, 'sign',
                     '--version', '0.0.0+0',
                     '--align', str(align),
                     '--header-size', str(vtoff),
                     '--slot-size', str(size)]
        sign_base.extend(args.tool_args)

        log.banner('signed binaries:')
        if in_bin:
            out_bin = args.sbin or str(b / 'zephyr' / 'zephyr.signed.bin')
            sign_bin = sign_base + [in_bin, out_bin]
            log.inf('bin: {}'.format(out_bin))
            log.dbg(quote_sh_list(sign_bin))
            subprocess.check_call(sign_bin)
        if in_hex:
            out_hex = args.shex or str(b / 'zephyr' / 'zephyr.signed.hex')
            sign_hex = sign_base + [in_hex, out_hex]
            log.inf('hex: {}'.format(out_hex))
            log.dbg(quote_sh_list(sign_hex))
            subprocess.check_call(sign_hex)

    @staticmethod
    def find_imgtool(command, args):
        if args.tool_path:
            command.check_force(shutil.which(args.tool_path),
                                '--tool-path {}: not an executable'.
                                format(args.tool_path))
            tool_path = args.tool_path
        else:
            tool_path = shutil.which('imgtool') or shutil.which('imgtool.py')
            if not tool_path:
                log.die('imgtool not found; either install it',
                        '(e.g. "pip3 install imgtool") or provide --tool-path')
        return tool_path

    @staticmethod
    def get_cfg(command, bcfg, item):
        try:
            return bcfg[item]
        except KeyError:
            command.check_force(
                False, "build .config is missing a {} value".format(item))
            return None

    @staticmethod
    def edt_flash_node(b):
        # Get the EDT Node corresponding to the zephyr,flash chosen DT
        # node; 'b' is the build directory as a pathlib object.

        # Ensure the build directory has a compiled DTS file
        # where we expect it to be.
        dts = b / 'zephyr' / 'zephyr.dts'
        log.dbg('DTS file:', dts, level=log.VERBOSE_VERY)
        edt_pickle = b / 'zephyr' / 'edt.pickle'
        if not edt_pickle.is_file():
            log.die("can't load devicetree; expected to find:", edt_pickle)

        # Load the devicetree.
        with open(edt_pickle, 'rb') as f:
            edt = pickle.load(f)

        # By convention, the zephyr,flash chosen node contains the
        # partition information about the zephyr image to sign.
        flash = edt.chosen_node('zephyr,flash')
        if not flash:
            log.die('devicetree has no chosen zephyr,flash node;',
                    "can't infer flash write block or image-0 slot sizes")

        return flash

    @staticmethod
    def edt_flash_params(flash):
        # Get the flash device's write alignment and the image-0
        # partition's size out of the build directory's devicetree.

        # The node must have a "partitions" child node, which in turn
        # must have a child node labeled "image-0". By convention, the
        # primary slot for consumption by imgtool is linked into this
        # partition.
        if 'partitions' not in flash.children:
            log.die("DT zephyr,flash chosen node has no partitions,",
                    "can't find partition for MCUboot slot 0")
        for node in flash.children['partitions'].children.values():
            if node.label == 'image-0':
                image_0 = node
                break
        else:
            log.die("DT zephyr,flash chosen node has no image-0 partition,",
                    "can't determine its size")

        # The partitions node, and its subnode, must provide
        # the size of the image-0 partition via the regs property.
        if not image_0.regs:
            log.die('image-0 flash partition has no regs property;',
                    "can't determine size of image slot 0")

        # Die on missing or zero alignment or slot_size.
        if "write-block-size" not in flash.props:
            log.die('DT zephyr,flash node has no write-block-size;',
                    "can't determine imgtool write alignment")
        align = flash.props['write-block-size'].val
        if align == 0:
            log.die('expected nonzero flash alignment, but got '
                    'DT flash device write-block-size {}'.format(align))
        reg = image_0.regs[0]
        if reg.size == 0:
            log.die('expected nonzero slot size, but got '
                    'DT image-0 partition size {}'.format(reg.size))

        return (align, reg.addr, reg.size)

class RimageSigner(Signer):

    def sign(self, command, build_dir, bcfg, formats):
        args = command.args

        if args.tool_path:
            command.check_force(shutil.which(args.tool_path),
                                '--tool-path {}: not an executable'.
                                format(args.tool_path))
            tool_path = args.tool_path
        else:
            tool_path = shutil.which('rimage')
            if not tool_path:
                log.die('rimage not found; either install it',
                        'or provide --tool-path')

        b = pathlib.Path(build_dir)
        cache = CMakeCache.from_build_dir(build_dir)

        board = cache['CACHED_BOARD']
        if board != 'up_squared_adsp':
            log.die('Supported only for up_squared_adsp board')

        log.inf('Signing with tool {}'.format(tool_path))

        bootloader = str(b / 'zephyr' / 'bootloader.elf.mod')
        kernel = str(b / 'zephyr' / 'zephyr.elf.mod')
        out_bin = str(b / 'zephyr' / 'zephyr.ri')

        sign_base = ([tool_path] + args.tool_args +
                     ['-o', out_bin, '-m', 'apl', '-i', '3'] +
                     [bootloader, kernel])

        log.inf(quote_sh_list(sign_base))
        subprocess.check_call(sign_base)
