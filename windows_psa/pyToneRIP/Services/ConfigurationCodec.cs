using System;
using System.IO;
using System.Runtime.Serialization.Json;
using System.Text;
using System.Threading.Tasks;
using pyToneRIP.Models;

namespace pyToneRIP.Services
{
    internal static class ConfigurationCodec
    {
        public static string Serialize(PyToneRipConfiguration configuration)
        {
            var settings = new DataContractJsonSerializerSettings { UseSimpleDictionaryFormat = true };
            var serializer = new DataContractJsonSerializer(typeof(PyToneRipConfiguration), settings);
            using (var stream = new MemoryStream())
            {
                serializer.WriteObject(stream, configuration);
                return Encoding.UTF8.GetString(stream.ToArray());
            }
        }

        public static PyToneRipConfiguration Deserialize(string json)
        {
            var settings = new DataContractJsonSerializerSettings { UseSimpleDictionaryFormat = true };
            var serializer = new DataContractJsonSerializer(typeof(PyToneRipConfiguration), settings);
            using (var stream = new MemoryStream(Encoding.UTF8.GetBytes(json)))
            {
                return (PyToneRipConfiguration)serializer.ReadObject(stream);
            }
        }

        public static Task<string> ReadStoredAsync(string printerUri, string printerName) =>
            Shared.QueueConfigurationStorage.ReadAsync(printerUri, printerName);

        public static Task WriteStoredAsync(
            string printerUri,
            string printerName,
            string configurationJson) =>
            Shared.QueueConfigurationStorage.WriteAsync(
                printerUri, printerName, configurationJson);
    }
}
