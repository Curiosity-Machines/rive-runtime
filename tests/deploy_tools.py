#!/usr/bin/python

import argparse
import atexit
import glob
import os
import platform
import queue
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

HANDSHAKE_TOKEN = 0xfee1600d
SHUTDOWN_TOKEN = 0xfee1dead

parser = argparse.ArgumentParser(description="Run native gms & goldens, and dump their .pngs")
parser.add_argument("tools",
                    type=str,
                    nargs="+",
                    choices=["gms", "goldens", "player"],
                    help="which tool(s) to run")
parser.add_argument("-B", "--builddir",
                    type=str,
                    default=None,
                    help="output directory from build")
parser.add_argument("-b", "--backend",
                    type=str,
                    default=None)
parser.add_argument("-s", "--src",
                    type=str,
                    default=os.path.join("..", "..", "..", "gold", "rivs"),
                    help="INPUT directory of .riv files to render")
parser.add_argument("-o", "--outdir",
                    type=str,
                    default=os.path.join(".gold", "candidates"),
                    help="base directory to output the PNG directory structure")
parser.add_argument("-p", "--png_threads",
                    type=int,
                    default=4,
                    help="Number of pngs encoding threads on each tool process.")
parser.add_argument("-j", "--jobs-per-tool",
                    type=int,
                    default=4,
                    help="number of processes to spawn for each tool in 'args.tools' "\
                         "(non-mobile only; android/ios only get one job)")
parser.add_argument("--rows",
                    type=int,
                    default=1,
                    help="number of rows in the goldens grid")
parser.add_argument("--cols",
                    type=int,
                    default=1,
                    help="number of columns in the goldens grid")
parser.add_argument("-m", "--match",
                    type=str,
                    default=None,
                    help="`match` patter for gms")
parser.add_argument("-t", "--target",
                    default="host",
                    choices=["host", "android", "ios", "iossim"],
                    help="which platform to run on")
parser.add_argument("-u", "--ios_udid",
                    type=str,
                    default=None,
                    help="unique id of iOS device to run on (--target=ios or iossim)")
parser.add_argument("-k", "--options",
                    type=str,
                    default=None,
                    help="additional options to pass through (player only)")
parser.add_argument("-S", "--server_only",
                    action='store_true',
                    help="Start servers but don't launch gms or goldens tools")
parser.add_argument("-r", "--remote",
                    action='store_true',
                    help="target is remote; serve from host IP instead of localhost")
parser.add_argument("--no-rebuild", action='store_true',
                    help="don't rebuild the native tools in builddir")
parser.add_argument("--no-install", action='store_true',
                    help="don't package & reinstall the mobile app prior to launch")
parser.add_argument("-v", "--verbose", action='store_true', help="enable verbose output")

args = parser.parse_args()
target_info = {} # dictionary for info about the target (ios_version, etc.)
rivsqueue = queue.Queue()

# Launch a process in a separate thread and crash if it fails.
class CheckProcess(threading.Thread):
    def __init__(self, cmd):
        threading.Thread.__init__(self)
        self.cmd = cmd
        if args.server_only:
            self.cmd = ["echo", "\n    <command> "] + ['"%s"' % arg for arg in self.cmd]

    def run(self):
        if args.verbose:
            print(' '.join(self.cmd), flush=True)
        proc = subprocess.Popen(self.cmd)
        proc.wait()
        if proc.returncode != 0:
            os._exit(proc.returncode)

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(0)
    try:
        # doesn't even have to be reachable
        s.connect(("10.254.254.254", 1))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip

# Simple TCP server for Rive tools.
class ToolServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True

    def __init__(self, handler):
        if args.remote:
            # The device needs to connect over the network instead of localhost.
            self.host = get_local_ip()
        else:
            self.host = "localhost" # Desktop and Android (with "adb reverse") can use local ports.
        address = (self.host, 0) # let the kernel give us a port
        self.shutdown_event = None
        self.claimed_gm_tests_lock = threading.Lock()
        self.claimed_gm_tests = set()
        super().__init__(address, handler)

    def server_activate(self):
        # Allow enough connections for each process we spawn.
        num_processes = len(args.tools) * args.jobs_per_tool
        # Each process establishes a maximum of:
        #   * 1 primary TestHarness server connection.
        #   * `png_threads` encoder connections.
        #   * 1 stdio-forwarding connection.
        threads_per_process = 1 + args.png_threads + 1
        self.socket.listen(num_processes * threads_per_process)

    def serve_forever_async(self):
        self.serve_thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.serve_thread.start()
        if self.host == "localhost" and args.target == "android":
            # Use adb port reverse-forwarding to expose our RIV server to the device.
            hostname, port = self.server_address # find out what port we were given
            subprocess.Popen(["adb", "reverse", "tcp:%s" % port, "tcp:%s" % port],
                             stdout=subprocess.DEVNULL)

    # Simple utility to wait until a TCP client tells the server it has finished.
    def wait_for_shutdown_event(self):
        if not self.shutdown_event.wait(timeout=5*60):
            print("error: 5 minute timeout waiting for the tool to finish! "
                  "Something is probably hung.", flush=True)
        self.shutdown_event = None

    def reset_shutdown_event(self):
        self.shutdown_event = threading.Event()

    # Only returns True the on the first request for a given name.
    # Prevents gms from running more than once in a multi-process execution.
    def claim_gm_test(self, name):
        with self.claimed_gm_tests_lock:
            if not name in self.claimed_gm_tests:
                self.claimed_gm_tests.add(name)
                return True
        return False

# RequestHandler with a "recvall" methd.
class ToolRequestHandler(socketserver.BaseRequestHandler):
    def recvall(self, byte_count):
        data = bytearray()
        while len(data) < byte_count:
            chunk = self.request.recv(min(byte_count - len(data), 4096))
            data.extend(chunk)
        return data

    def recv_string(self):
        length = int.from_bytes(self.recvall(4), byteorder="big")
        return self.recvall(length).decode("ascii")

# RequestHandler with services for Rive tools.
class TestHarnessRequestHandler(ToolRequestHandler):
    REQUEST_TYPE_IMAGE_UPLOAD = 0
    REQUEST_TYPE_CLAIM_GM_TEST = 1
    REQUEST_TYPE_CONSOLE_MESSAGE = 2
    REQUEST_TYPE_DISCONNECT = 3
    REQUEST_TYPE_APPLICATION_CRASH = 4

    def handle(self):
        try:
            while True:
                # Receive the next request.
                requesttype = int.from_bytes(self.recvall(4), byteorder="big")
                if requesttype == self.REQUEST_TYPE_DISCONNECT:
                    shutdown = int.from_bytes(self.recvall(4), byteorder="big")
                    if self.server and shutdown:
                        if self.server.shutdown_event:
                            self.server.shutdown_event.set()
                    break

                elif requesttype == TestHarnessRequestHandler.REQUEST_TYPE_IMAGE_UPLOAD:
                    destination = os.path.join(args.outdir, self.recv_string())

                    with open(destination, "wb") as pngfile:
                        while True:
                            chunksize = int.from_bytes(self.recvall(4), byteorder="big")
                            if chunksize == HANDSHAKE_TOKEN:
                                break
                            pngfile.write(self.recvall(chunksize))

                    self.request.sendall(HANDSHAKE_TOKEN.to_bytes(4, byteorder="big"))

                    if args.verbose:
                        print("[server] Received %s" % destination, flush=True)

                elif requesttype == TestHarnessRequestHandler.REQUEST_TYPE_CLAIM_GM_TEST:
                    shouldrun = self.server.claim_gm_test(self.recv_string())
                    self.request.sendall(shouldrun.to_bytes(4, byteorder="big"))

                elif requesttype == TestHarnessRequestHandler.REQUEST_TYPE_CONSOLE_MESSAGE:
                    print(self.recv_string(), end="", flush=True)

                elif requesttype == TestHarnessRequestHandler.REQUEST_TYPE_APPLICATION_CRASH:
                    print("CRASH in tool: %s" % self.recv_string(), flush=True)
                    os._exit(-1)

        except ConnectionResetError:
            print("TestHarness connection reset by client tool", flush=True)
            os._exit(-1)

# Sends a new .riv file to a ready client.
class RIVRequestHandler(ToolRequestHandler):
    def handle(self):
        try:
            while True:
                remaining = rivsqueue.qsize()
                if not args.verbose and remaining % 7 == 0:
                    print("[%3u] rivs remaining...\r" % remaining,
                          end='\r' if not args.verbose else '', flush=True)

                riv = rivsqueue.get_nowait()
                if args.verbose:
                    print("[server] Sending %s..." % riv, end='\r', flush=True)

                # Send the next riv to the client.
                riv_ascii = os.path.basename(riv).encode("ascii")
                self.request.sendall(len(riv_ascii).to_bytes(4, byteorder="big"))
                self.request.sendall(riv_ascii)
                host_filename = riv
                with open(host_filename, "rb") as rivfile:
                    rivbytes = rivfile.read()
                    self.request.sendall(len(rivbytes).to_bytes(4, byteorder="big"))
                    self.request.sendall(rivbytes)

                # Wait for the client to tell us it has finished before sending the next .riv.
                handshake = int.from_bytes(self.recvall(4), byteorder="big")
                if handshake != HANDSHAKE_TOKEN:
                    print("Bad handshake", flush=True)
                    os._exit(-1)

            self.request.sendall(HANDSHAKE_TOKEN.to_bytes(4, byteorder="big"))

        except queue.Empty:
            # .rivs are finished. Tell the client to shutdown.
            self.request.sendall(SHUTDOWN_TOKEN.to_bytes(4, byteorder="big"))

        except ConnectionResetError:
            print("RIV server connection reset by tool", flush=True)
            os._exit(-1)

# If we aren't deploying to the host, update the given command to deploy on its intended target.
def update_cmd_to_deploy_on_target(cmd):
    dirname = os.path.dirname(cmd[0])
    toolname = os.path.basename(cmd[0])

    if args.target == "android":
        sharedlib = os.path.join(dirname, "lib%s.so" % toolname)
        print("\nDeploying %s on android..." % sharedlib)
        tool_args = ' '.join([sharedlib] + cmd[1:])
        return ["adb", "shell",
                "am force-stop app.rive.android_tools && "
                "am start -n app.rive.android_tools/.%s -e args '%s'" % (toolname, tool_args)]

    elif args.target == "ios":
        print("\nDeploying %s on ios (udid=%s, ios_version=%i)..." %
              (toolname, args.ios_udid, target_info["ios_version"]))
        cmd = [toolname] + cmd[1:]
        if target_info["ios_version"] >= 17:
            # ios-deploy is no longer supported after iOS 17.
            return ["xcrun", "devicectl", "device", "process", "launch",
                    # "--console",  # TODO: "--console" not currently supported.
                    "--device", args.ios_udid,
                    "--environment-variables", '{"MTL_DEBUG_LAYER": "1"}',
                    "rive.app.golden-test-app"] + cmd
        else:
            return ["ios-deploy", "--noinstall", "--noninteractive", "--bundle",
                    "ios_tools/build/Debug-iphoneos/rive_ios_tools.app",
                    "--envs", "MTL_DEBUG_LAYER=1",
                    "--args", ' '.join(cmd)]

    elif args.target == "iossim":
        print("\nDeploying %s on ios simulator (udid=%s)..." % (toolname, args.ios_udid))
        cmd = [toolname] + cmd[1:]
        return ["xcrun", "simctl", "launch", args.ios_udid, "rive.app.golden-test-app"] + cmd

    else:
        assert(args.target == "host")
        return cmd

def launch_gms(test_harness_server):
    cmd = [os.path.join(args.builddir, "gms"),
           "--backend", args.backend,
           "--output", "%s:%u" % test_harness_server.server_address,
           "--headless",
           "-p%i" % args.png_threads]
    if args.match:
        cmd = cmd + ["--match", args.match];
    if args.verbose:
        cmd = cmd + ["--verbose"];
    cmd = update_cmd_to_deploy_on_target(cmd)

    procs = [CheckProcess(cmd) for i in range(0, args.jobs_per_tool)]
    for proc in procs:
        proc.start()

    return procs


def launch_goldens(test_harness_server, riv_server):
    tool = os.path.join(args.builddir, "goldens")
    if args.verbose:
        print("[server] Using '" + tool + "'", flush=True)

    if not os.path.exists(args.src):
        print("Can't find rivspath " + args.src, flush=True)
        return -1;

    src = glob.glob(os.path.join(args.src, "*.riv"))
    n = len(src)

    for riv in src:
        rivsqueue.put(riv)

    cmd = [tool,
           "--output", "%s:%u" % test_harness_server.server_address,
           "--src", "%s:%u" % riv_server.server_address,
           "--backend", args.backend,
           "--rows", str(args.rows),
           "--cols", str(args.cols),
           "--headless",
           "-p%i" % args.png_threads]
    if args.verbose:
        cmd = cmd + ["--verbose"];
    cmd = update_cmd_to_deploy_on_target(cmd)

    procs = [CheckProcess(cmd) for i in range(0, args.jobs_per_tool)]
    for proc in procs:
        proc.start()

    return procs

def launch_player(test_harness_server, riv_server):
    if not os.path.exists(args.src):
        print("Can't find riv path " + args.src, flush=True)
        return -1;

    rivsqueue.put(args.src)
    cmd = [os.path.join(args.builddir, "player"),
           "--output", "%s:%u" % test_harness_server.server_address,
           "--src", "%s:%u" % riv_server.server_address,
           "--backend", args.backend]
    if args.options:
        cmd += ["--options", args.options]
    cmd = update_cmd_to_deploy_on_target(cmd)

    player = CheckProcess(cmd)
    player.start()
    return player

def force_stop_android_tools_apk():
    subprocess.check_call(["adb", "shell", "am force-stop app.rive.android_tools"])

def main():
    if args.target == "android":
        args.jobs_per_tool = 1 # Android can only launch one process at a time.
        if args.builddir == None:
            args.builddir = "out/android_arm64_debug"
        if args.backend == None:
            args.backend = "gl"
    elif args.target == "ios":
        if args.builddir == None:
            args.builddir = "out/ios_debug"
        elif args.builddir != "out/ios_debug":
            print("The iOS wrapper app requires --builddir=out/ios_debug")
            return -1
        if args.backend == None:
            args.backend = "metal"
        args.jobs_per_tool = 1 # iOS can only launch one process at a time.
        args.remote = True # Since we can't do port forwarding in iOS, it always has to be remote.
        if not args.ios_udid:
            args.ios_udid = subprocess.check_output(["idevice_id", "-l"]).decode().strip()
        device_info = os.popen("xcrun xctrace list devices | grep %s" % args.ios_udid).read()
        target_info["ios_version"] = int(re.search(r".+\(([0-9]+)\.[0-9\.]+\).+$",
                                                   device_info).group(1))
    elif args.target == "iossim":
        if args.builddir == None:
            args.builddir = "out/iossim_universal_debug"
        elif args.builddir != "out/iossim_universal_debug":
            print("The iOS-simulator wrapper app requires --builddir=out/iossim_universal_debug")
            return -1
        if args.backend == None:
            args.backend = "metal"
        args.jobs_per_tool = 1 # iOS can only launch one process at a time.
        args.remote = True # Since we can't do port forwarding in iOS, it always has to be remote.
        if not args.ios_udid:
            args.ios_udid = "booted"
    else:
        assert(args.target == "host")
        if args.builddir == None:
            args.builddir = "out/debug"
        if args.backend == None:
            args.backend = "metal" if platform.system() == "Darwin" else \
                           "d3d" if platform.system() == "Windows" else \
                           "gl"

    if "metal" in args.backend:
        # Turn on Metal validation layers.
        # NOTE: MoltenVK generates Metal validation errors right now, so only them on for our own
        # Metal backends.
        os.environ["MTL_DEBUG_LAYER"] = "1"

    if args.server_only:
        args.jobs_per_tool = 1 # Only print the command for each job once.

    # Build the native tools.
    if not args.no_rebuild and not args.no_install:
        rive_tools_dir = os.path.dirname(os.path.realpath(__file__))
        build_rive = [os.path.join(rive_tools_dir, "../build/build_rive.sh")]
        if os.name == "nt":
            if subprocess.run(["which", "msbuild.exe"]).returncode == 0:
                # msbuild.exe is already on the $PATH; launch build_rive.sh directly.
                build_rive = ["sh"] + build_rive
            else:
                # msbuild.exe is not on the path; go through build_rive.bat.
                build_rive[0] = os.path.splitext(build_rive[0])[0] + '.bat'
        if "ios" in args.target:
            # ios links statically, so we need to build every tool every time.
            build_targets = ["gms", "goldens", "player"]
        else:
            build_targets = args.tools
        subprocess.check_call(build_rive + ["rebuild", args.builddir] + build_targets)

    if not args.no_install:
        if args.target == "android":
            # Copy the native libraries into the android_tools project.
            jni_dir = os.path.join("android_tools", "app", "src", "main", "jniLibs")
            android_arch = "arm64-v8a" # TODO: support more android architectures if needed.
            os.makedirs(os.path.join(jni_dir, android_arch), exist_ok=True)
            for tool in build_targets:
                sharedlib = "lib%s.so" % tool
                shutil.copy(os.path.join(args.builddir, sharedlib), os.path.join(jni_dir, android_arch))
            layerpath = os.path.join(jni_dir, android_arch, "libVkLayer_khronos_validation.so")
            if args.backend in ["vk", "vulkan", "sw", "swiftshader"] and not os.path.exists(layerpath):
                # Download & bundle the Vulkan validation layers.
                print("Downloading Android Vulkan validation layers...", flush=True)
                url = "https://github.com/KhronosGroup/Vulkan-ValidationLayers/releases/download/"\
                      "vulkan-sdk-1.3.290.0/android-binaries-1.3.290.0.zip"
                zipfile.ZipFile(urllib.request.urlretrieve(url)[0], 'r').extractall()
                for lib in glob.glob("android-binaries-1.3.290.0/**/*.so", recursive=True):
                    dst = lib.replace("android-binaries-1.3.290.0", jni_dir)
                    print("  bundling %s -> %s" % (lib, dst), flush=True)
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.move(lib, dst)
                shutil.rmtree("android-binaries-1.3.290.0")
            # Build the android_tools wrapper app.
            cwd = os.getcwd()
            os.chdir(os.path.join(rive_tools_dir, "android_tools"))
            subprocess.check_call(["./gradlew" if os.name != "nt" else "gradlew.bat",
                                   ":app:assembleDebug"])
            # Install the android_tools wrapper app.
            force_stop_android_tools_apk()
            subprocess.check_call(["adb", "install", "-r", "app/build/outputs/apk/debug/app-debug.apk"])
            os.chdir(cwd)
            print()
        elif args.target == "ios":
            # Build the ios_tools wrapper app.
            subprocess.check_call(["xcodebuild",
                                   "-destination", "generic/platform=iOS",
                                   "-config", "Debug",
                                   "build", "-project", "ios_tools/ios_tools.xcodeproj"])
            # Install the ios_tools wrapper app on the device.
            if target_info["ios_version"] >= 17:
                # ios-deploy is no longer supported after iOS 17.
                subprocess.check_call(["xcrun", "devicectl", "device", "install", "app", "--device",
                                       args.ios_udid,
                                       "ios_tools/build/Debug-iphoneos/rive_ios_tools.app"])
            else:
                subprocess.check_call(["ios-deploy", "--bundle",
                                       "ios_tools/build/Debug-iphoneos/rive_ios_tools.app"])
            print()
        elif args.target == "iossim":
            # Build the ios_tools wrapper app for the simulator.
            subprocess.check_call(["xcodebuild",
                                   "-destination", "generic/platform=iOS Simulator",
                                   "-config", "Debug",
                                   "-sdk", "iphonesimulator",
                                   "build", "-project", "ios_tools/ios_tools.xcodeproj"])
            # Install the ios_tools wrapper app on the simulator.
            subprocess.check_call(["xcrun", "simctl", "install", args.ios_udid,
                                   "ios_tools/build/Debug-iphonesimulator/rive_ios_tools.app"])
            print()

    if args.target == "android":
        atexit.register(force_stop_android_tools_apk)

    with (ToolServer(TestHarnessRequestHandler) as test_harness_server,
          ToolServer(RIVRequestHandler) as riv_server):
        test_harness_server.serve_forever_async()
        print("TestHarness server running on %s:%u" % test_harness_server.server_address,
              flush=True)

        riv_server.serve_forever_async()
        print("RIV server running on %s:%u" % riv_server.server_address, flush=True)

        # On mobile we can't launch >1 instance of the app at a time.
        serial_deploy = not args.server_only and ("ios" in args.target or args.target == "android")
        parallel_procs = []

        if "gms" in args.tools:
            os.makedirs(args.outdir, exist_ok=True)
            if serial_deploy:
                test_harness_server.reset_shutdown_event()
                gms = launch_gms(test_harness_server)
                assert(len(gms) == 1)
                test_harness_server.wait_for_shutdown_event()
                gms[0].join()
            else:
                parallel_procs += launch_gms(test_harness_server)

        if "goldens" in args.tools:
            os.makedirs(args.outdir, exist_ok=True)
            if serial_deploy:
                test_harness_server.reset_shutdown_event()
                goldens = launch_goldens(test_harness_server, riv_server)
                assert(len(goldens) == 1)
                test_harness_server.wait_for_shutdown_event()
                goldens[0].join()
            else:
                parallel_procs += launch_goldens(test_harness_server, riv_server)

        if "player" in args.tools:
            if serial_deploy:
                test_harness_server.reset_shutdown_event()
                player = launch_player(test_harness_server, riv_server)
                test_harness_server.wait_for_shutdown_event()
                player.join()
            else:
                parallel_procs += [launch_player(test_harness_server, riv_server)]

        # Wait for the parallel processes to finish (if not in serial_deploy mode).
        for proc in parallel_procs:
            proc.join()

        if args.server_only:
            # Sleep until user input.
            input("\nPress enter to shutdown...")

        print("done                          ", flush=True)

        riv_server.shutdown()
        test_harness_server.shutdown()

    return 0

if __name__ == "__main__":
    sys.exit(main())