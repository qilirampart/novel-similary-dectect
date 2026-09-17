import { useEffect, useRef, useState } from "react";

import {
  getCoverMonitorFilterOptions,
  type CoverFilterOptionsResponse
} from "../api";


const EMPTY_OPTIONS: CoverFilterOptionsResponse = {
  operators: [],
  channels: []
};

export function CoverScopeFilters({
  operatorPk,
  channelPk,
  disabled = false,
  onOperatorChange,
  onChannelChange
}: {
  operatorPk?: number;
  channelPk?: number;
  disabled?: boolean;
  onOperatorChange: (value?: number) => void;
  onChannelChange: (value?: number) => void;
}) {
  const [options, setOptions] = useState<CoverFilterOptionsResponse>(EMPTY_OPTIONS);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const requestRef = useRef(0);

  useEffect(() => {
    const requestId = ++requestRef.current;
    setLoading(true);
    setError("");
    void getCoverMonitorFilterOptions(operatorPk)
      .then((response) => {
        if (requestId === requestRef.current) setOptions(response);
      })
      .catch((loadError) => {
        if (requestId !== requestRef.current) return;
        setError(loadError instanceof Error ? loadError.message : "筛选项加载失败");
      })
      .finally(() => {
        if (requestId === requestRef.current) setLoading(false);
      });
  }, [operatorPk]);

  return (
    <div className="cover-scope-filters">
      <label>
        <span>代理归属</span>
        <select
          aria-label="按代理归属筛选"
          value={operatorPk ?? ""}
          disabled={disabled || loading}
          onChange={(event) => {
            const next = event.target.value === "" ? undefined : Number(event.target.value);
            onOperatorChange(next);
            onChannelChange(undefined);
          }}
        >
          <option value="">全部代理</option>
          {options.operators.map((item) => (
            <option value={item.operator_pk} key={item.operator_pk}>
              {item.name}（{item.channel_count}）
            </option>
          ))}
        </select>
      </label>
      <label>
        <span>频道</span>
        <select
          aria-label="按频道筛选"
          value={channelPk ?? ""}
          disabled={disabled || loading || operatorPk == null}
          onChange={(event) => onChannelChange(event.target.value === "" ? undefined : Number(event.target.value))}
        >
          <option value="">{operatorPk == null ? "请先选择代理" : "全部频道"}</option>
          {options.channels.map((item) => (
            <option value={item.channel_pk} key={item.channel_pk}>
              {item.name} · {item.channel_id}
            </option>
          ))}
        </select>
      </label>
      {error && <small role="alert">{error}</small>}
    </div>
  );
}
