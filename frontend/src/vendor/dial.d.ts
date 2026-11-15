export interface DialSection {
  zh: string;
  en?: string;
  href?: string;
  big: number | string;
  unit?: string;
  sub?: string;
  here?: boolean;
  alert?: boolean;
}

export interface DialGauge {
  label: string;
  share: number;
}

export interface DialProps {
  sections: DialSection[];
  gauges?: DialGauge[];
  idle?: DialSection;
}

export function drawDial(svg: SVGSVGElement, props: DialProps): void;
