// Anonymous statistics of measured neutral ramps; generated offline.
// Linearized print curves need the measured endpoints, not a second correction.
// Source-set SHA-256: 5172d9fe8bbfb5886f8bd1f9cd33d4c55ba3754b30c020b490f4a2214a73a316
namespace pyToneRIP.Services
{
    internal struct PaperRange
    {
        public readonly double Black, White;
        public readonly int Samples;
        public PaperRange(double black, double white, int samples)
        { Black = black; White = white; Samples = samples; }
    }

    internal static class PreviewPaperMeasurements
    {
        public static PaperRange Get(string category)
        {
            switch (category)
            {
                case "baryta": return new PaperRange(6.255, 97.545, 20);
                case "fineart": return new PaperRange(17.0464, 97.56, 120);
                case "glossy": return new PaperRange(6.35, 95.1, 112);
                case "luster": return new PaperRange(6.1494, 95.2051, 25);
                case "matte": return new PaperRange(18.1423, 95.8042, 94);
                case "plain": return new PaperRange(27.4, 93.8, 188);
                case "watercolor": return new PaperRange(16.8946, 96.191, 4);
                // No measured identity: retain an explicit broad estimate.
                default: return new PaperRange(18, 95, 0);
            }
        }
    }
}
