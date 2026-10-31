using System;

namespace pyToneRIP.Services
{
    // Small transfer tables only. Full-page pixel processing is native Direct2D.
    internal static class PreviewToneMapping
    {
        public static float[] Gamma(int gamma)
        {
            if (gamma < -15 || gamma > 15) throw new ArgumentOutOfRangeException(nameof(gamma));
            var table = new float[256];
            var exponent = Math.Log(0.5 + gamma / 100.0) / Math.Log(0.5);
            for (var i = 0; i < table.Length; i++)
                table[i] = (float)(1 - Math.Floor(255 * Math.Pow((255-i)/255.0, exponent) + 0.5)/255);
            return table;
        }

        public static float[][] ApproximatePaper(string medium, bool oba, int illuminantStep = 0)
        {
            if (illuminantStep < 0 || illuminantStep > 3) throw new ArgumentOutOfRangeException(nameof(illuminantStep));
            var measured = PreviewPaperMeasurements.Get(PaperCategory(medium));
            // Measurements contain L*, not spectral reflectance or a*/b*.
            // The OBA tint remains an explicit approximation, not a measured
            // fluorescence model. Quad linearization already supplies tone shape.
            var black = measured.Black;
            var light = IlluminantMatrix(illuminantStep);
            var result = new[] { new float[256], new float[256], new float[256] };
            for (var i = 0; i < 256; i++)
            {
                var t = i / 255.0;
                var l = black + (measured.White - black) * t;
                // Adapted paper appearance, not measured substrate a*/b*.
                // Keep a modest paper-white distinction independent of the
                // common viewing-light transform. No UV response is inferred.
                var a = (oba ? -0.15 : 0.25) * t;
                var b = (oba ? -3.0 : 2.0) * t;
                var fy = (l+16)/116;
                var x = 0.96422 * LabInverse(fy+a/500);
                var y = LabInverse(fy);
                var z = 0.82521 * LabInverse(fy-b/200);
                // D50 Lab -> D65 monitor reference. The illuminant control
                // approximates viewing adaptation; it never changes print data.
                var xd = 0.9555766*x - 0.0230393*y + 0.0631636*z;
                var yd = -0.0282895*x + 1.0099416*y + 0.0210077*z;
                var zd = 0.0122982*x - 0.0204830*y + 1.3299098*z;
                var r = 3.2404542*xd - 1.5371385*yd - 0.4985314*zd;
                var g = -0.9692660*xd + 1.8760108*yd + 0.0415560*zd;
                var blue = 0.0556434*xd - 0.2040259*yd + 1.0572252*zd;
                var adapted = Apply(light, new[] { r, g, blue });
                // Compress chroma toward neutral at constant luminance instead
                // of clipping each channel: clipping erased the OBA difference.
                var luminance = Math.Max(0, Math.Min(1, .2126729*adapted[0]+.7151522*adapted[1]+.0721750*adapted[2]));
                var chroma = 1.0;
                foreach (var channel in adapted)
                {
                    var delta = channel-luminance;
                    if (channel > 1) chroma = Math.Min(chroma, (1-luminance)/delta);
                    else if (channel < 0) chroma = Math.Min(chroma, -luminance/delta);
                }
                for (var channel = 0; channel < 3; channel++)
                    result[channel][i] = Encode(luminance+chroma*(adapted[channel]-luminance));
            }
            return result;
        }

        public static string PaperCategory(string medium)
        {
            var name = (medium ?? "").ToLowerInvariant().Replace(" ", "").Replace("_", "").Replace("-", "");
            if (name.Contains("watercolor") || name.Contains("watercolour") || name.Contains("etching")) return "watercolor";
            if (name.Contains("canvas")) return "canvas";
            if (name.Contains("baryta")) return "baryta";
            if (name.Contains("luster") || name.Contains("lustre") || name.Contains("pearl") || name.Contains("semigloss") || name.Contains("satin")) return "luster";
            if (name.Contains("fineart") || name.Contains("velvet") || name.Contains("photorag") || name.Contains("museum")) return "fineart";
            if (name.Contains("plain") || name == "normal") return "plain";
            if (name.Contains("matte")) return "matte";
            if (name.Contains("photo") || name.Contains("gloss")) return "glossy";
            return "generic";
        }

        public static double[] IlluminantMatrix(int step)
        {
            if (step < 0 || step > 3) throw new ArgumentOutOfRangeException(nameof(step));
            if (step == 0) return new[] { 1.0,0,0, 0,1.0,0, 0,0,1.0 };
            // Bradford cone-space adaptation. Most of the illuminant shift is
            // discounted by visual adaptation; retain a 10% residual as an
            // explicit viewing assumption, not a measured surround/CAM model.
            var bradford = new[] { .8951,.2664,-.1614, -.7502,1.7135,.0367, .0389,-.0685,1.0296 };
            var inverse = new[] { .9869929,-.1470543,.1599627, .4323053,.5183603,.0492912, -.0085287,.0400428,.9684867 };
            var toXyz = new[] { .4124564,.3575761,.1804375, .2126729,.7151522,.0721750, .0193339,.1191920,.9503041 };
            var toRgb = new[] { 3.2404542,-1.5371385,-.4985314, -.9692660,1.8760108,.0415560, .0556434,-.2040259,1.0572252 };
            var reference = Apply(bradford, DaylightWhite(5000));
            var selected = Apply(bradford, DaylightWhite(5000+500*step));
            var cones = new double[9];
            for (var i = 0; i < 3; i++) cones[i*3+i] = Math.Pow(selected[i]/reference[i], .10);
            return Multiply(toRgb, Multiply(inverse, Multiply(cones, Multiply(bradford, toXyz))));
        }

        private static double[] DaylightWhite(double temperature)
        {
            var x = -4.607e9/Math.Pow(temperature,3) + 2.9678e6/Math.Pow(temperature,2) + 99.11/temperature + .244063;
            var y = -3*x*x + 2.87*x - .275;
            return new[] { x/y, 1.0, (1-x-y)/y };
        }

        private static double[] Apply(double[] matrix, double[] value) => new[] {
            matrix[0]*value[0]+matrix[1]*value[1]+matrix[2]*value[2],
            matrix[3]*value[0]+matrix[4]*value[1]+matrix[5]*value[2],
            matrix[6]*value[0]+matrix[7]*value[1]+matrix[8]*value[2] };

        private static double[] Multiply(double[] left, double[] right)
        {
            var result = new double[9];
            for (var row = 0; row < 3; row++)
                for (var column = 0; column < 3; column++)
                    for (var i = 0; i < 3; i++) result[row*3+column] += left[row*3+i]*right[i*3+column];
            return result;
        }

        private static double LabInverse(double value) => value > 6.0/29
            ? value*value*value : 3*(6.0/29)*(6.0/29)*(value-4.0/29);
        private static float Encode(double value) => (float)Math.Max(0, Math.Min(1,
            value <= 0.0031308 ? 12.92*value : 1.055*Math.Pow(value, 1.0/2.4)-0.055));
    }
}
