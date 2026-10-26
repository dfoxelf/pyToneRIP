using System;
using System.ComponentModel;
using System.IO;
using System.Management;
using System.Xml;

namespace pyToneRIP.QueueHelper
{
    internal static class ConfigurationTicketXmlReader
    {
        // Get-PrintConfiguration uses this same inbox CIM class and method.
        // Read it directly in the existing desktop helper, as the current user:
        // no PowerShell process, elevation, or DEVMODE-to-PrintTicket conversion.
        // The result is queue configuration, not proof of per-user defaults.
        public static string Read(string queueName)
        {
            if (string.IsNullOrWhiteSpace(queueName))
                throw new ArgumentException("Windows queue name is required.", nameof(queueName));
            var scope = new ManagementScope(@"\\.\root\StandardCimv2");
            using (var provider = new ManagementClass(
                scope, new ManagementPath("MSFT_PrinterConfiguration"), null))
            using (var parameters = provider.GetMethodParameters("GetByPrinterName"))
            {
                parameters["PrinterName"] = queueName;
                var options = new InvokeMethodOptions { Timeout = TimeSpan.FromSeconds(10) };
                using (var result = provider.InvokeMethod("GetByPrinterName", parameters, options))
                {
                    if (result == null || result["ReturnValue"] == null)
                        throw new InvalidDataException("Windows CIM returned no configuration result.");
                    var status = Convert.ToUInt32(result["ReturnValue"]);
                    if (status != 0)
                        throw new Win32Exception(unchecked((int)status),
                            "Windows CIM could not read the printer configuration XML.");
                    using (var configuration = result["CmdletOutput"] as ManagementBaseObject)
                    {
                        if (configuration == null || !string.Equals(
                            configuration["PrinterName"] as string, queueName,
                            StringComparison.OrdinalIgnoreCase))
                            throw new InvalidDataException("Windows CIM returned a different printer queue.");
                        var xml = configuration["PrintTicketXML"] as string;
                        Validate(xml);
                        return xml;
                    }
                }
            }
        }

        internal static void Validate(string xml)
        {
            if (string.IsNullOrWhiteSpace(xml))
                throw new InvalidDataException("Windows CIM returned an empty PrintTicket XML.");
            var settings = new XmlReaderSettings
            {
                DtdProcessing = DtdProcessing.Prohibit,
                XmlResolver = null,
            };
            var document = new XmlDocument { XmlResolver = null };
            using (var text = new StringReader(xml))
            using (var reader = XmlReader.Create(text, settings))
                document.Load(reader);
            var root = document.DocumentElement;
            if (root == null || root.LocalName != "PrintTicket" || root.NamespaceURI !=
                "http://schemas.microsoft.com/windows/2003/08/printing/printschemaframework")
                throw new InvalidDataException("Windows CIM did not return a Print Schema PrintTicket.");
        }
    }
}
