using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Linq;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.RegularExpressions;
using pyToneRIP.Models;

namespace pyToneRIP.QueueHelper
{
    internal static class UsbPrinterDiscovery
    {
        private const uint Success = 0;
        private const uint BufferSmall = 0x1A;
        private const uint PresentEnumerator = 0x101;
        private const uint FriendlyNameProperty = 0x0D;
        private const uint DeviceDescriptionProperty = 0x01;

        internal static List<UsbPrinterDescriptor> Discover()
        {
            var ports = ReadUsbPorts();
            var queues = PrinterApi.ListLocalPrinters();
            var result = new List<UsbPrinterDescriptor>();
            foreach (var id in ReadPresentDeviceIds())
            {
                uint node;
                if (CM_Locate_DevNodeW(out node, id, 0) != Success) continue;
                var parts = id.Split('\\');
                if (parts.Length < 3) continue;
                var model = parts[1];
                var friendly = ReadProperty(node, FriendlyNameProperty)
                    ?? ReadProperty(node, DeviceDescriptionProperty) ?? model.Replace('_', ' ');
                if (!model.StartsWith("EPSON", StringComparison.OrdinalIgnoreCase)
                    && !friendly.StartsWith("EPSON", StringComparison.OrdinalIgnoreCase)) continue;
                var match = Regex.Match(parts[2], @"(?:^|&)(USB\d+)$", RegexOptions.IgnoreCase);
                var portName = match.Success ? match.Groups[1].Value.ToUpperInvariant() : null;
                string portDescription;
                if (portName != null && ports.TryGetValue(portName, out portDescription))
                {
                    if (string.Equals(friendly, "Generic printer device", StringComparison.OrdinalIgnoreCase))
                        friendly = string.IsNullOrWhiteSpace(portDescription) ? model.Replace('_', ' ') : portDescription;
                }
                else portName = null;
                result.Add(new UsbPrinterDescriptor
                {
                    DeviceInstanceId = id,
                    ModelName = model,
                    DisplayName = friendly,
                    PortName = portName,
                    QueueNames = queues.Where(queue => portName != null
                        && queue.RawUsbReady
                        && string.Equals(queue.PortName, portName, StringComparison.OrdinalIgnoreCase)
                        && !queue.Name.StartsWith("pyToneRIP", StringComparison.OrdinalIgnoreCase)
                        && !string.Equals(queue.DriverName, "Microsoft Virtual Print Class Driver", StringComparison.OrdinalIgnoreCase)
                        && !string.Equals(queue.DriverName, "Microsoft IPP Class Driver", StringComparison.OrdinalIgnoreCase))
                        .Select(queue => queue.Name).OrderBy(name => name, StringComparer.OrdinalIgnoreCase).ToList(),
                });
            }
            return result.OrderBy(item => item.DisplayName, StringComparer.OrdinalIgnoreCase)
                .ThenBy(item => item.DeviceInstanceId, StringComparer.OrdinalIgnoreCase).ToList();
        }

        private static IEnumerable<string> ReadPresentDeviceIds()
        {
            // Retry only enumeration: a printer can arrive between size and list.
            for (var attempt = 0; attempt < 3; attempt++)
            {
                uint size;
                var status = CM_Get_Device_ID_List_SizeW(out size, "USBPRINT", PresentEnumerator);
                if (status != Success) throw new InvalidOperationException("USB device enumeration failed: " + status);
                if (size <= 1) return new string[0];
                if (size > 1024 * 1024) throw new InvalidOperationException("USB device list is too large.");
                var buffer = new char[size];
                status = CM_Get_Device_ID_ListW("USBPRINT", buffer, size, PresentEnumerator);
                if (status == BufferSmall) continue;
                if (status != Success) throw new InvalidOperationException("USB device enumeration failed: " + status);
                return new string(buffer).Split(new[] { '\0' }, StringSplitOptions.RemoveEmptyEntries);
            }
            throw new InvalidOperationException("USB devices changed during enumeration. Refresh the list.");
        }

        private static string ReadProperty(uint node, uint property)
        {
            uint type, bytes = 2048;
            var buffer = new byte[bytes];
            var status = CM_Get_DevNode_Registry_PropertyW(node, property, out type, buffer, ref bytes, 0);
            return status == Success && bytes > 2
                ? Encoding.Unicode.GetString(buffer, 0, (int)bytes).TrimEnd('\0') : null;
        }

        private static Dictionary<string, string> ReadUsbPorts()
        {
            uint needed, returned;
            var succeeded = EnumPortsW(null, 2, IntPtr.Zero, 0, out needed, out returned);
            var error = Marshal.GetLastWin32Error();
            if (needed == 0)
            {
                if (!succeeded && error != 0) throw new Win32Exception(error, "Could not read USB printer ports.");
                return new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            }
            if (error != 122) throw new Win32Exception(error, "Could not read USB printer ports.");
            var buffer = Marshal.AllocHGlobal(checked((int)needed));
            try
            {
                if (!EnumPortsW(null, 2, buffer, needed, out needed, out returned))
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not read USB printer ports.");
                var ports = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
                var stride = Marshal.SizeOf(typeof(PortInfo2));
                for (var index = 0; index < returned; index++)
                {
                    var port = (PortInfo2)Marshal.PtrToStructure(IntPtr.Add(buffer, index * stride), typeof(PortInfo2));
                    if (port.Name != null && Regex.IsMatch(port.Name, @"^USB\d+$", RegexOptions.IgnoreCase))
                        ports[port.Name] = port.Description;
                }
                return ports;
            }
            finally { Marshal.FreeHGlobal(buffer); }
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct PortInfo2
        {
            [MarshalAs(UnmanagedType.LPWStr)] public string Name;
            [MarshalAs(UnmanagedType.LPWStr)] public string Monitor;
            [MarshalAs(UnmanagedType.LPWStr)] public string Description;
            public uint Type;
            public uint Reserved;
        }

        [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
        private static extern uint CM_Get_Device_ID_List_SizeW(out uint size, string filter, uint flags);
        [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
        private static extern uint CM_Get_Device_ID_ListW(string filter, [Out] char[] buffer, uint length, uint flags);
        [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
        private static extern uint CM_Locate_DevNodeW(out uint node, string id, uint flags);
        [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
        private static extern uint CM_Get_DevNode_Registry_PropertyW(uint node, uint property, out uint type, [Out] byte[] buffer, ref uint bytes, uint flags);
        [DllImport("winspool.drv", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool EnumPortsW(string server, uint level, IntPtr ports, uint bytes, out uint needed, out uint returned);
    }
}
