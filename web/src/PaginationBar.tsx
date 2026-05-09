import { Icon } from "./icons";

type PaginationBarProps = {
  page: number;
  pageCount: number;
  total: number;
  pageSize: number;
  itemLabel: string;
  onChange: (page: number) => void;
  className?: string;
};

export function PaginationBar({
  page,
  pageCount,
  total,
  pageSize,
  itemLabel,
  onChange,
  className = ""
}: PaginationBarProps) {
  const safePageCount = Math.max(pageCount, 1);
  const safePage = Math.min(Math.max(page, 1), safePageCount);
  const start = total === 0 ? 0 : (safePage - 1) * pageSize + 1;
  const end = total === 0 ? 0 : Math.min(safePage * pageSize, total);

  return (
    <div className={`dashboard-pagination compact ${className}`.trim()}>
      <button
        className="outline-button slim pagination-button"
        type="button"
        onClick={() => onChange(safePage - 1)}
        disabled={safePage <= 1}
      >
        <Icon name="arrow" className="pagination-arrow-icon is-left" />
        上一页
      </button>
      <div className="pagination-summary">
        <strong>第 {safePage} / {safePageCount} 页</strong>
        <span>{total === 0 ? `暂无${itemLabel}` : `显示 ${start}-${end} / 共 ${total} 条${itemLabel}`}</span>
      </div>
      <button
        className="outline-button slim pagination-button"
        type="button"
        onClick={() => onChange(safePage + 1)}
        disabled={safePage >= safePageCount}
      >
        下一页
        <Icon name="arrow" className="pagination-arrow-icon" />
      </button>
    </div>
  );
}
