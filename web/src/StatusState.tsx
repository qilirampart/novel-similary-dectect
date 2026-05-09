import { Icon } from "./icons";
import type { IconName } from "./types";

type StateTone = "neutral" | "info" | "warning" | "error";

type StatusStateProps = {
  title: string;
  description?: string;
  tone?: StateTone;
  variant?: "panel" | "inline" | "dark";
  icon?: IconName;
  className?: string;
};

export function StatusState({
  title,
  description = "",
  tone = "neutral",
  variant = "panel",
  icon,
  className = ""
}: StatusStateProps) {
  const iconName = icon ?? (tone === "error" ? "warning" : tone === "warning" ? "filter" : "doc");
  const classes = [
    "status-state",
    `status-state-${variant}`,
    `tone-${tone}`,
    className
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <div className={classes}>
      <div className="status-state-icon">
        <Icon name={iconName} />
      </div>
      <div className="status-state-copy">
        <strong>{title}</strong>
        {description ? <p>{description}</p> : null}
      </div>
    </div>
  );
}
