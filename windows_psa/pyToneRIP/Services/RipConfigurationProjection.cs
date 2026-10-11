using System;
using Windows.Data.Json;

namespace pyToneRIP.Shared
{
    // A queue configuration also contains Windows UI state. The job record
    // carries a separate, explicit contract with only RIP inputs. Windows DPI
    // is taken from the job PrintTicket when converting XPS to TIFF.
    internal static class RipConfigurationProjection
    {
        internal const string Schema = "pytone-rip.rip-configuration";

        internal static JsonObject FromQueueJson(string queueJson)
        {
            var configuration = JsonObject.Parse(queueJson);
            if (configuration.GetNamedString("schema", "") != "pytone-rip.configuration"
                || configuration.GetNamedNumber("version", 0) != 1)
                throw new ArgumentException("Queue configuration schema is invalid.", nameof(queueJson));

            var printer = configuration.GetNamedObject("printer");
            printer.Remove("raster_resolution_x");
            printer.Remove("raster_resolution_y");
            configuration.SetNamedValue("schema", JsonValue.CreateStringValue(Schema));
            return configuration;
        }
    }
}
