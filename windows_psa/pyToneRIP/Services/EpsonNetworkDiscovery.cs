using System;
using System.Collections.Generic;
using System.Linq;
using System.Runtime.InteropServices.WindowsRuntime;
using System.Threading;
using System.Threading.Tasks;
using pyToneRIP.Models;
using Windows.Networking;
using Windows.Networking.Sockets;
using Windows.Storage.Streams;

namespace pyToneRIP.Services
{
    internal sealed class NetworkPrinterDescriptor
    {
        public string Host { get; }
        public string ModelName { get; }
        public bool RawPortReachable { get; }

        public NetworkPrinterDescriptor(string host, string modelName, bool rawPortReachable)
        {
            Host = host;
            ModelName = modelName;
            RawPortReachable = rawPortReachable;
        }
    }

    internal static class EpsonNetworkDiscovery
    {
        private const string EnpcPort = "3289";
        private const int ProbeReplySize = 76;
        private const int MaxResponders = 64;
        // The 15-byte ENPC probe is also used by SANE's epson2 backend.
        private static readonly byte[] Probe =
            { 0x45, 0x50, 0x53, 0x4f, 0x4e, 0x50, 0x00, 0xff, 0, 0, 0, 0, 0, 0, 0 };

        internal static bool MatchesModel(PrinterCapability model, string reportedName, bool allowHqFallback = true)
        {
            if (model == null) return false;
            return UsbPrinterModelMatcher.MatchesReportedModel(model.Id, model.DisplayName, reportedName)
                || (model.DisplayName ?? "").Split('/').Skip(1).Any(alias =>
                    UsbPrinterModelMatcher.MatchesReportedModel(null, alias, reportedName))
                || (allowHqFallback && model.Id?.EndsWith("_HQ", StringComparison.Ordinal) == true
                    && UsbPrinterModelMatcher.MatchesReportedModel(
                        model.Id.Substring(0, model.Id.Length - 3), null, reportedName));
        }

        internal static async Task<IReadOnlyList<NetworkPrinterDescriptor>> DiscoverAsync(
            CancellationToken cancellation)
        {
            var hosts = await DiscoverHostsAsync(cancellation);
            using (var slots = new SemaphoreSlim(8))
            {
                var printers = await Task.WhenAll(
                    hosts.Select(host => IdentifyAsync(host, slots, cancellation)));
                return printers.OrderBy(printer => printer.ModelName, StringComparer.OrdinalIgnoreCase)
                    .ThenBy(printer => printer.Host, StringComparer.Ordinal).ToList();
            }
        }

        private static async Task<string[]> DiscoverHostsAsync(CancellationToken cancellation)
        {
            var hosts = new HashSet<string>(StringComparer.Ordinal);
            using (var socket = new DatagramSocket())
            using (var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellation))
            {
                deadline.CancelAfter(TimeSpan.FromSeconds(4));
                socket.MessageReceived += (_, args) =>
                {
                    if (args.RemotePort != EnpcPort || args.RemoteAddress?.Type != HostNameType.Ipv4)
                        return;
                    var host = args.RemoteAddress.CanonicalName;
                    if (host.StartsWith("127.", StringComparison.Ordinal)) return;
                    try
                    {
                        using (var reader = args.GetDataReader())
                        {
                            if (reader.UnconsumedBufferLength < 12 || reader.UnconsumedBufferLength > 2048)
                                return;
                            var reply = new byte[reader.UnconsumedBufferLength];
                            reader.ReadBytes(reply);
                            if (!IsDiscoveryReply(reply)) return;
                        }
                        lock (hosts)
                        {
                            if (hosts.Count < MaxResponders)
                                hosts.Add(host);
                        }
                    }
                    catch (Exception) { /* An invalid datagram cannot interrupt discovery. */ }
                };

                // Unconnected WinRT UDP broadcasts on the available interfaces
                // and receives unicast replies on the same ephemeral port.
                await socket.BindServiceNameAsync("").AsTask(deadline.Token);
                using (var output = await socket.GetOutputStreamAsync(
                    new HostName("255.255.255.255"), EnpcPort).AsTask(deadline.Token))
                using (var writer = new DataWriter(output))
                {
                    foreach (var query in new[] { Probe, PrinterConnectivityService.EnpcQuery })
                    {
                        writer.WriteBytes(query);
                        await writer.StoreAsync().AsTask(deadline.Token);
                    }
                    await Task.Delay(TimeSpan.FromSeconds(2), deadline.Token);
                    writer.DetachStream();
                }
                lock (hosts) return hosts.ToArray();
            }
        }

        internal static bool IsDiscoveryReply(byte[] reply) =>
            PrinterConnectivityService.IsEnpcReply(reply)
            || (reply != null && reply.Length == ProbeReplySize &&
                reply[0] == 'E' && reply[1] == 'P' && reply[2] == 'S' &&
                reply[3] == 'O' && reply[4] == 'N');

        private static async Task<NetworkPrinterDescriptor> IdentifyAsync(
            string host, SemaphoreSlim slots, CancellationToken cancellation)
        {
            await slots.WaitAsync(cancellation);
            try
            {
                var model = PrinterSnmpService.ReadModelNameAsync(host, cancellation);
                var raw = PrinterConnectivityService.TestRawTcpPortAsync(host, PrinterInstanceStore.RawTcpPort);
                await Task.WhenAll(model, raw);
                return new NetworkPrinterDescriptor(host, await model, await raw == PrinterNetworkState.Online);
            }
            finally { slots.Release(); }
        }
    }
}
