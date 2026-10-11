using System;
using System.Collections.Generic;
using System.Runtime.InteropServices.WindowsRuntime;
using System.Threading;
using System.Threading.Tasks;
using Windows.Networking;
using Windows.Networking.Sockets;
using Windows.Storage.Streams;

namespace pyToneRIP.Services
{
    internal enum PrinterNetworkState
    {
        Unknown,
        Checking,
        Online,
        Preparing,
        Queued,
        Processing,
        Printing,
        Offline,
    }

    internal static class PrinterConnectivityService
    {
        // Epson discovery uses UDP 3289 and does not touch the print stream.
        // The XP-960 and ET-2550 both answered this unicast query.
        private static readonly byte[] EnpcQuery =
            { 0x45, 0x50, 0x53, 0x4f, 0x4e, 0x51, 0x03, 0, 0, 0, 0x10, 0, 0, 0, 0, 0 };
        private static readonly Dictionary<string, int> MissedByHost =
            new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
        private static readonly HashSet<string> SeenByHost =
            new HashSet<string>(StringComparer.OrdinalIgnoreCase);

        public static async Task<PrinterNetworkState> TestAsync(string host, int port)
        {
            if (string.IsNullOrWhiteSpace(host) || port < 1 || port > 65535)
                return PrinterNetworkState.Unknown;

            host = host.Trim();
            var response = await QueryUdpAsync(host, "3289", EnpcQuery,
                TimeSpan.FromSeconds(2));
            if (IsEnpcReply(response))
            {
                lock (MissedByHost)
                {
                    MissedByHost[host] = 0;
                    SeenByHost.Add(host);
                }
                return PrinterNetworkState.Online;
            }

            // A model without ENPC may still expose SNMP. Neither management
            // timeout by itself means that the RAW print service is offline.
            var snmpState = await PrinterSnmpService.ReadStatusAsync(host);
            if (snmpState.HasValue)
            {
                lock (MissedByHost)
                {
                    MissedByHost[host] = 0;
                    SeenByHost.Add(host);
                }
                return snmpState.Value;
            }

            lock (MissedByHost)
            {
                int missed;
                MissedByHost.TryGetValue(host, out missed);
                missed++;
                MissedByHost[host] = missed;
                return SeenByHost.Contains(host) && missed >= 3
                    ? PrinterNetworkState.Offline : PrinterNetworkState.Unknown;
            }
        }

        // Called only by the explicit Test button beside port 9100. The
        // periodic table monitor continues to use ENPC/SNMP exclusively.
        public static async Task<PrinterNetworkState> TestRawTcpPortAsync(
            string host, int port, TimeSpan? timeout = null)
        {
            if (string.IsNullOrWhiteSpace(host) || port < 1 || port > 65535)
                return PrinterNetworkState.Unknown;

            using (var socket = new StreamSocket())
            using (var cancellation = new CancellationTokenSource(
                timeout ?? TimeSpan.FromSeconds(2)))
            {
                try
                {
                    await socket.ConnectAsync(
                        new HostName(host.Trim()),
                        port.ToString(System.Globalization.CultureInfo.InvariantCulture),
                        SocketProtectionLevel.PlainSocket).AsTask(cancellation.Token);
                    return PrinterNetworkState.Online;
                }
                catch (Exception)
                {
                    return PrinterNetworkState.Offline;
                }
            }
        }

        private static bool IsEnpcReply(byte[] response) =>
            response != null && response.Length >= 12 &&
            response[0] == (byte)'E' && response[1] == (byte)'P' &&
            response[2] == (byte)'S' && response[3] == (byte)'O' &&
            response[4] == (byte)'N' && response[5] == (byte)'q' &&
            response[6] == 3;

        internal static async Task<byte[]> QueryUdpAsync(
            string host, string port, byte[] request, TimeSpan timeout)
        {
            using (var socket = new DatagramSocket())
            using (var cancellation = new CancellationTokenSource(timeout))
            {
                var reply = new TaskCompletionSource<byte[]>(
                    TaskCreationOptions.RunContinuationsAsynchronously);
                socket.MessageReceived += (sender, args) =>
                {
                    try
                    {
                        if (!string.Equals(args.RemoteAddress?.CanonicalName, host,
                            StringComparison.OrdinalIgnoreCase)) return;
                        var reader = args.GetDataReader();
                        var bytes = new byte[reader.UnconsumedBufferLength];
                        reader.ReadBytes(bytes);
                        reply.TrySetResult(bytes);
                    }
                    catch (Exception) { /* malformed datagram */ }
                };
                try
                {
                    await socket.ConnectAsync(new HostName(host), port)
                        .AsTask(cancellation.Token);
                    using (var writer = new DataWriter(socket.OutputStream))
                    {
                        writer.WriteBytes(request);
                        await writer.StoreAsync().AsTask(cancellation.Token);
                        writer.DetachStream();
                    }
                    var completed = await Task.WhenAny(reply.Task,
                        Task.Delay(timeout, cancellation.Token));
                    return completed == reply.Task ? await reply.Task : null;
                }
                catch (Exception)
                {
                    return null;
                }
            }
        }
    }
}
