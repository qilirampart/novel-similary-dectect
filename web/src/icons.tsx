import type { IconName } from "./types";

type IconProps = {
  name: IconName;
  className?: string;
};

export function Icon({ name, className }: IconProps) {
  const strokeWidth = 1.8;
  switch (name) {
    case "dashboard":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <rect x="4" y="4" width="6" height="6" rx="1.5" stroke="currentColor" strokeWidth={strokeWidth} />
          <rect x="14" y="4" width="6" height="6" rx="1.5" stroke="currentColor" strokeWidth={strokeWidth} />
          <rect x="4" y="14" width="6" height="6" rx="1.5" stroke="currentColor" strokeWidth={strokeWidth} />
          <rect x="14" y="14" width="6" height="6" rx="1.5" stroke="currentColor" strokeWidth={strokeWidth} />
        </svg>
      );
    case "search":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <circle cx="11" cy="11" r="6.5" stroke="currentColor" strokeWidth={strokeWidth} />
          <path d="M16 16l4 4" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" />
        </svg>
      );
    case "layers":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M12 4l8 4-8 4-8-4 8-4z" stroke="currentColor" strokeWidth={strokeWidth} strokeLinejoin="round" />
          <path d="M4 12l8 4 8-4" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" />
          <path d="M4 16l8 4 8-4" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" />
        </svg>
      );
    case "review":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M7 4h10a2 2 0 0 1 2 2v12l-4-2-4 2-4-2-4 2V6a2 2 0 0 1 2-2h2z" stroke="currentColor" strokeWidth={strokeWidth} strokeLinejoin="round" />
          <path d="M9 9h6M9 12h6" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" />
        </svg>
      );
    case "tasks":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <rect x="5" y="4" width="14" height="16" rx="2.5" stroke="currentColor" strokeWidth={strokeWidth} />
          <path d="M9 8h6M9 12h6M9 16h4" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" />
        </svg>
      );
    case "rules":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M6 8h12M6 16h12" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" />
          <circle cx="9" cy="8" r="2" fill="currentColor" />
          <circle cx="15" cy="16" r="2" fill="currentColor" />
        </svg>
      );
    case "database":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <ellipse cx="12" cy="6.5" rx="7" ry="3.5" stroke="currentColor" strokeWidth={strokeWidth} />
          <path d="M5 6.5v11c0 1.9 3.1 3.5 7 3.5s7-1.6 7-3.5v-11" stroke="currentColor" strokeWidth={strokeWidth} />
          <path d="M5 12c0 1.9 3.1 3.5 7 3.5s7-1.6 7-3.5" stroke="currentColor" strokeWidth={strokeWidth} />
        </svg>
      );
    case "logs":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <rect x="5" y="4" width="14" height="16" rx="2.5" stroke="currentColor" strokeWidth={strokeWidth} />
          <path d="M9 9h6M9 13h6M9 17h4" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" />
        </svg>
      );
    case "status":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M5 18V7a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v11" stroke="currentColor" strokeWidth={strokeWidth} />
          <path d="M8 15l2-2 2 1 4-4" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" />
          <path d="M4 19h16" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" />
        </svg>
      );
    case "bell":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M7 10a5 5 0 1 1 10 0v4l2 2H5l2-2v-4z" stroke="currentColor" strokeWidth={strokeWidth} strokeLinejoin="round" />
          <path d="M10 19a2 2 0 0 0 4 0" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" />
        </svg>
      );
    case "user":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <circle cx="12" cy="8" r="3.5" stroke="currentColor" strokeWidth={strokeWidth} />
          <path d="M5 19c1.7-3 4.1-4.5 7-4.5s5.3 1.5 7 4.5" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" />
        </svg>
      );
    case "spark":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M12 3l1.7 5.3L19 10l-5.3 1.7L12 17l-1.7-5.3L5 10l5.3-1.7L12 3z" stroke="currentColor" strokeWidth={strokeWidth} strokeLinejoin="round" />
        </svg>
      );
    case "upload":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M12 15V5M8.5 8.5L12 5l3.5 3.5" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" />
          <path d="M5 19h14" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" />
        </svg>
      );
    case "shield":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M12 4l7 3v5c0 4-2.5 6.8-7 8-4.5-1.2-7-4-7-8V7l7-3z" stroke="currentColor" strokeWidth={strokeWidth} strokeLinejoin="round" />
          <path d="M9.5 12l1.7 1.7L15 10" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      );
    case "warning":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M12 4l8 14H4l8-14z" stroke="currentColor" strokeWidth={strokeWidth} strokeLinejoin="round" />
          <path d="M12 9v4M12 16h.01" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" />
        </svg>
      );
    case "clock":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <circle cx="12" cy="12" r="8" stroke="currentColor" strokeWidth={strokeWidth} />
          <path d="M12 8v5l3 2" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      );
    case "file":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M8 4h6l4 4v12H8a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z" stroke="currentColor" strokeWidth={strokeWidth} strokeLinejoin="round" />
          <path d="M14 4v4h4" stroke="currentColor" strokeWidth={strokeWidth} strokeLinejoin="round" />
        </svg>
      );
    case "refresh":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M19 8V4h-4" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" />
          <path d="M19 4l-4.2 4.2A7 7 0 1 0 19 12" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      );
    case "chart":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M5 18l4-5 3 2 5-7 2 2" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" />
          <path d="M4 19h16" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" />
        </svg>
      );
    case "check":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M6 12.5l4 4L18 8.5" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      );
    case "arrow":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M9 6l6 6-6 6" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      );
    case "filter":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M5 7h14M8 12h8M10 17h4" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" />
        </svg>
      );
    case "pulse":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M4 13h4l2-4 4 8 2-4h4" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      );
    case "book":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M6 5.5A2.5 2.5 0 0 1 8.5 3H18v16H8.5A2.5 2.5 0 0 0 6 21V5.5z" stroke="currentColor" strokeWidth={strokeWidth} />
          <path d="M6 5.5A2.5 2.5 0 0 0 3.5 8V19H18" stroke="currentColor" strokeWidth={strokeWidth} />
        </svg>
      );
    case "queue":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M7 6h10M7 12h10M7 18h10" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" />
          <circle cx="4" cy="6" r="1.4" fill="currentColor" />
          <circle cx="4" cy="12" r="1.4" fill="currentColor" />
          <circle cx="4" cy="18" r="1.4" fill="currentColor" />
        </svg>
      );
    case "doc":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M8 4h7l4 4v11a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2z" stroke="currentColor" strokeWidth={strokeWidth} />
          <path d="M14 4v4h4" stroke="currentColor" strokeWidth={strokeWidth} />
          <path d="M9 12h6M9 15h6" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" />
        </svg>
      );
    case "expand":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M8 4H4v4" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" />
          <path d="M4 4l6 6" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" />
          <path d="M16 4h4v4" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" />
          <path d="M20 4l-6 6" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" />
          <path d="M4 16v4h4" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" />
          <path d="M4 20l6-6" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" />
          <path d="M20 16v4h-4" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" />
          <path d="M20 20l-6-6" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" />
        </svg>
      );
    case "close":
      return (
        <svg className={className} viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round" />
        </svg>
      );
    default:
      return null;
  }
}
