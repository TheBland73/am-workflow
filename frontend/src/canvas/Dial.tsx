import { useEffect, useRef } from "react";
import { drawDial, type DialGauge, type DialSection } from "../vendor/dial.js";

export default function Dial({
  sections, gauges, idle,
}: {
  sections: DialSection[];
  gauges: DialGauge[];
  idle?: DialSection;
}) {
  const ref = useRef<SVGSVGElement>(null);

  useEffect(() => {
    if (ref.current && sections.length) {
      drawDial(ref.current, { sections, gauges, idle });
    }
  }, [sections, gauges, idle]);

  return (
    <svg
      ref={ref}
      className="ef-dial"
      role="group"
      aria-label="八个相位"
      onClickCapture={(event) => {
        if ((event.target as Element).closest("a")) event.preventDefault();
      }}
    />
  );
}
