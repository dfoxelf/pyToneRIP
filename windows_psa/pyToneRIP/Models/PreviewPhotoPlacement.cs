using System;
using System.Runtime.Serialization;

namespace pyToneRIP.Models
{
    internal static class PreviewPrintDirection
    {
        // Logical page edge mapped to the physical first raster row.
        // PageOrientation Landscape rotates content counterclockwise.
        internal static int LeadingEdge(string orientation)
        {
            var name = orientation ?? "Portrait";
            name = name.Substring(name.LastIndexOf(':')+1);
            switch (name.ToLowerInvariant())
            {
                case "landscape": return 1;
                case "reverseportrait": return 2;
                case "reverselandscape": return 3;
                default: return 0;
            }
        }
    }

    [DataContract]
    internal sealed class PreviewPhotoPlacement
    {
        [DataMember(Name = "page_index")] public int PageIndex { get; set; }
        [DataMember(Name = "x_mm")] public double X { get; set; }
        [DataMember(Name = "y_mm")] public double Y { get; set; }
        [DataMember(Name = "width_mm")] public double Width { get; set; }
        [DataMember(Name = "height_mm")] public double Height { get; set; }
        [DataMember(Name = "rotation_degrees")] public int Rotation { get; set; }

        public PreviewPhotoPlacement Copy() => (PreviewPhotoPlacement)MemberwiseClone();

        public void Validate(double paperWidth, double paperHeight)
        {
            foreach (var value in new[] { X, Y, Width, Height, paperWidth, paperHeight })
                if (double.IsNaN(value) || double.IsInfinity(value)) throw new ArgumentException("Photo placement must be finite.");
            if (Rotation != 0 && Rotation != 90 && Rotation != 180 && Rotation != 270)
                throw new ArgumentException("Photo rotation must be 0, 90, 180 or 270 degrees.");
            if (PageIndex < 0 || Width <= 0 || Height <= 0 || paperWidth <= 0 || paperHeight <= 0)
                throw new ArgumentException("Invalid photo or paper dimensions.");
            if (X < -.0001 || Y < -.0001 || X+Width > paperWidth+.0001 || Y+Height > paperHeight+.0001)
                throw new ArgumentException(string.Format(System.Globalization.CultureInfo.InvariantCulture,
                    "Photo placement exceeds the {0:0.###} x {1:0.###} mm sheet.", paperWidth, paperHeight));
        }

        public void Move(double dx, double dy, double paperWidth, double paperHeight)
        {
            X = Clamp(X+dx, 0, paperWidth-Width); Y = Clamp(Y+dy, 0, paperHeight-Height);
        }

        // Corner bits: 1 = right, 2 = bottom. Keep the opposite corner fixed
        // and preserve the current aspect ratio during a mouse resize.
        public void Resize(int corner, double dx, double dy, double paperWidth, double paperHeight)
        {
            var right = (corner & 1) != 0; var bottom = (corner & 2) != 0;
            var anchorX = right ? X : X+Width; var anchorY = bottom ? Y : Y+Height;
            var fx = (Width+(right ? dx : -dx))/Width;
            var fy = (Height+(bottom ? dy : -dy))/Height;
            var factor = Math.Abs(fx-1) >= Math.Abs(fy-1) ? fx : fy;
            var maximum = Math.Min((right ? paperWidth-anchorX : anchorX)/Width,
                (bottom ? paperHeight-anchorY : anchorY)/Height);
            factor = Clamp(factor, Math.Min(maximum, Math.Max(1/Width, 1/Height)), maximum);
            Width *= factor; Height *= factor;
            X = right ? anchorX : anchorX-Width; Y = bottom ? anchorY : anchorY-Height;
        }

        public void Center(bool horizontal, double paperWidth, double paperHeight)
        {
            Validate(paperWidth, paperHeight);
            if (horizontal) X = Math.Max(0, (paperWidth-Width)/2);
            else Y = Math.Max(0, (paperHeight-Height)/2);
        }

        public void Rotate(int direction, double paperWidth, double paperHeight)
        {
            var cx = X+Width/2; var cy = Y+Height/2;
            var width = Height; var height = Width;
            var fit = Math.Min(1, Math.Min(paperWidth/width, paperHeight/height));
            Width = width*fit; Height = height*fit;
            X = Clamp(cx-Width/2, 0, paperWidth-Width); Y = Clamp(cy-Height/2, 0, paperHeight-Height);
            Rotation = (Rotation+(direction < 0 ? 270 : 90))%360;
        }

        public void ApplyPageBox(double x, double y, double width, double height, double paperWidth, double paperHeight)
        {
            X = x+X/paperWidth*width; Y = y+Y/paperHeight*height;
            Width *= width/paperWidth; Height *= height/paperHeight;
        }

        public double[] Matrix(double sourceWidth, double sourceHeight, double unitsPerMm)
        {
            var x = X*unitsPerMm; var y = Y*unitsPerMm;
            var w = Width*unitsPerMm; var h = Height*unitsPerMm;
            switch (Rotation)
            {
                case 90: return new[] { 0, h/sourceWidth, -w/sourceHeight, 0, x+w, y };
                case 180: return new[] { -w/sourceWidth, 0, 0, -h/sourceHeight, x+w, y+h };
                case 270: return new[] { 0, -h/sourceWidth, w/sourceHeight, 0, x, y+h };
                default: return new[] { w/sourceWidth, 0, 0, h/sourceHeight, x, y };
            }
        }

        private static double Clamp(double value, double minimum, double maximum) => Math.Max(minimum, Math.Min(maximum, value));
    }

    // The PDL converter can rotate XPS onto a portrait PDF sheet. Editing
    // coordinates must stay in the immutable original XPS page's axes.
    internal sealed class PreviewSourcePage
    {
        public double Width { get; }
        public double Height { get; }
        public int Rotation { get; }
        private readonly double pdfWidth, pdfHeight;

        public PreviewSourcePage(double pdfWidth, double pdfHeight,
            double xpsWidthMm, double xpsHeightMm, bool reverseLandscape)
        {
            foreach (var value in new[] { pdfWidth, pdfHeight, xpsWidthMm, xpsHeightMm })
                if (value <= 0 || double.IsNaN(value) || double.IsInfinity(value))
                    throw new ArgumentException("Invalid preview source page size.");
            this.pdfWidth = pdfWidth; this.pdfHeight = pdfHeight;
            var swapped = (pdfWidth > pdfHeight) != (xpsWidthMm > xpsHeightMm);
            Rotation = swapped ? (reverseLandscape ? 270 : 90) : 0;
            Width = swapped ? pdfHeight : pdfWidth;
            Height = swapped ? pdfWidth : pdfHeight;
        }

        public double[] PdfToSourceMatrix()
        {
            if (Rotation == 90) return new[] { 0.0, 1, -1, 0, pdfHeight, 0 };
            if (Rotation == 270) return new[] { 0.0, -1, 1, 0, 0, pdfWidth };
            return new[] { 1.0, 0, 0, 1, 0, 0 };
        }

        public bool MatchesPaper(double widthMm, double heightMm)
        {
            // Catalog PPD points are rounded (e.g. A4 595.2 x 841.8), while
            // the PDF retains full dimensions. That is not a paper edit.
            return Math.Abs(Width*25.4/96-widthMm) < .1
                && Math.Abs(Height*25.4/96-heightMm) < .1;
        }
    }

}
