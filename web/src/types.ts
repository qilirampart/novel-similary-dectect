export type NavItem = {
  label: string;
  path: string;
  icon: IconName;
};

export type TopNavItem = {
  label: string;
  path: string;
};

export type IconName =
  | "dashboard"
  | "search"
  | "layers"
  | "review"
  | "tasks"
  | "rules"
  | "database"
  | "logs"
  | "status"
  | "bell"
  | "user"
  | "spark"
  | "upload"
  | "shield"
  | "warning"
  | "clock"
  | "file"
  | "refresh"
  | "chart"
  | "check"
  | "arrow"
  | "filter"
  | "pulse"
  | "book"
  | "queue"
  | "doc"
  | "expand"
  | "close";
