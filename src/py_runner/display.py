"""Collects rich display objects during execution. Standard library only.

Third-party libraries (matplotlib, pandas, numpy, PIL, cv2) are all
optional and probed lazily — no hard dependencies.
"""

import base64
import io

from .utils import try_import


class DisplayCollector:
    """Collects rich display objects during execution."""

    #: Hard cap per display item to avoid MBs of JSON from huge reprs.
    MAX_ITEM_LENGTH = 50000

    def __init__(self, max_length: int = 50000):
        self.items = []
        self.max_length = max_length

    def add(self, mime_type: str, data: str):
        if isinstance(data, str) and len(data) > self.max_length:
            data = data[: self.max_length] + (
                f"\n... [truncated at {self.max_length} chars]"
            )
        self.items.append({"type": mime_type, "data": data})

    def capture_matplotlib(self):
        plt = try_import("matplotlib.pyplot")
        if plt is None:
            return
        for fig_num in plt.get_fignums():
            fig = plt.figure(fig_num)
            buf = io.BytesIO()
            fig.savefig(buf, format="png", bbox_inches="tight", dpi=100)
            buf.seek(0)
            self.add("image/png", base64.b64encode(buf.read()).decode())
            plt.close(fig)

    def capture_pil_image(self, img) -> bool:
        """Capture a PIL Image object."""
        try:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            self.add("image/png", base64.b64encode(buf.read()).decode())
            return True
        except Exception:
            return False

    def capture_cv2_image(self, img) -> bool:
        """Capture an OpenCV image (numpy array in BGR)."""
        try:
            import cv2

            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            pil = try_import("PIL.Image")
            if pil:
                image = pil.fromarray(rgb)
                return self.capture_pil_image(image)
        except Exception:
            pass
        return False

    def capture_last_expression(self, result, max_length: int | None = None):
        if result is None:
            return
        limit = max_length if max_length is not None else self.max_length

        def _trunc(text: str) -> str:
            if len(text) > limit:
                return text[:limit] + f"\n... [truncated at {limit} chars]"
            return text

        pd = try_import("pandas")
        np = try_import("numpy")
        PIL_Image = try_import("PIL.Image")

        if pd and isinstance(result, pd.DataFrame):
            self.add("text/html", result.to_html(max_rows=20, max_cols=20))
            self.add("text/plain", _trunc(repr(result)))
        elif pd and isinstance(result, pd.Series):
            self.add("text/html", result.to_frame().to_html(max_rows=20))
            self.add("text/plain", _trunc(repr(result)))
        elif PIL_Image and isinstance(result, PIL_Image.Image):
            self.capture_pil_image(result)
        elif np and isinstance(result, np.ndarray):
            # Check if it looks like an image (H, W, 3) or (H, W, 4)
            if result.ndim == 3 and result.shape[2] in (3, 4):
                PIL_img = try_import("PIL.Image")
                if PIL_img:
                    try:
                        self.capture_pil_image(
                            PIL_img.fromarray(result.astype("uint8"))
                        )
                        return
                    except Exception:
                        pass
            self.add(
                "text/plain",
                _trunc(f"ndarray(shape={result.shape}, dtype={result.dtype})\n{repr(result)}"),
            )
        else:
            summary = self._tensor_summary(result)
            if summary is not None:
                self.add("text/plain", summary)
                return
            text = repr(result)
            if text != "None":
                self.add("text/plain", _trunc(text))

    @staticmethod
    def _tensor_summary(result) -> str | None:
        """Short summary for torch/jax/tf tensors without hard deps."""
        try:
            mod = type(result).__module__ or ""
            cls = type(result).__name__
            is_tensor = (
                ("torch" in mod)
                or ("jax" in mod)
                or ("tensorflow" in mod)
                or ("tensor" in cls.lower())
            )
            if not is_tensor:
                return None
            shape = getattr(result, "shape", None)
            dtype = getattr(result, "dtype", None)
            device = getattr(result, "device", None)
            parts = [f"{mod}.{cls}" if mod else cls]
            if shape is not None:
                parts.append(f"shape={tuple(shape) if hasattr(shape, '__iter__') else shape}")
            if dtype is not None:
                parts.append(f"dtype={dtype}")
            if device is not None:
                parts.append(f"device={device}")
            return ", ".join(parts)
        except Exception:
            return None


# Backwards-compat alias (old private name).
_DisplayCollector = DisplayCollector
