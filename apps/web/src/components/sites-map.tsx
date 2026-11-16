"use client";

import { useState, useEffect } from "react";
import maplibregl from "maplibre-gl";
import Map, { Marker, NavigationControl, Popup } from "react-map-gl/maplibre";
import "maplibre-gl/dist/maplibre-gl.css";
import { TIER, pct, type SiteStatus } from "@/lib/occupational";

export const STYLE = "https://api.maptiler.com/maps/dataviz-light/style.json?key=5sXuSxvFqLtJvmh5bokl";

export default function SitesMap({ sites }: { sites: SiteStatus[] }) {
  const [selected, setSelected] = useState<SiteStatus | null>(null);

  // Next.js 16 + MapLibre: pre-warm the worker class so the bundler resolves it
  useEffect(() => {
    // @ts-expect-error — MapLibre's workerClass is not in the type defs
    if (typeof window !== "undefined" && !maplibregl.workerClass) {
      // @ts-expect-error — not in the type defs (see above)
      maplibregl.workerClass = null;
    }
  }, []);

  return (
    <div className="relative h-[440px] w-full overflow-hidden rounded-xl ring-1 ring-foreground/10">
      <Map
        initialViewState={{ latitude: 24.5, longitude: 54.5, zoom: 7 }}
        mapStyle={STYLE}
        style={{ width: "100%", height: "100%" }}
        attributionControl={{ compact: true }}
      >
        <NavigationControl position="top-right" showCompass={false} />
        {sites.map((s) => (
          <Marker key={s.site_id} longitude={s.lon} latitude={s.lat} anchor="center">
            <button
              type="button"
              title={`${s.name} — ${TIER[s.risk_tier].label}`}
              aria-label={`${s.name}, ${TIER[s.risk_tier].label} risk`}
              data-site={s.site_id}
              data-tier={s.risk_tier}
              onClick={(e) => {
                e.stopPropagation();
                setSelected(s);
              }}
              className="block h-4 w-4 cursor-pointer rounded-full border-2 border-white shadow-md transition-transform hover:scale-125"
              style={{ background: TIER[s.risk_tier].hex }}
            />
          </Marker>
        ))}
        {selected && (
          <Popup
            longitude={selected.lon}
            latitude={selected.lat}
            anchor="bottom"
            offset={12}
            closeOnClick={false}
            onClose={() => setSelected(null)}
          >
            <div className="text-xs text-slate-900">
              <div className="font-semibold">{selected.name}</div>
              <div className="text-slate-500">{selected.operator}</div>
              <div className="mt-1">
                {selected.worker_count.toLocaleString()} workers · absence {pct(selected.absence_rate)}
              </div>
              <div>
                Feels-like max {selected.max_apparent_temp_today?.toFixed(0) ?? "—"}°C · PM2.5{" "}
                {selected.mean_pm25_today?.toFixed(0) ?? "—"}
              </div>
            </div>
          </Popup>
        )}
      </Map>
      <div className="pointer-events-none absolute bottom-3 left-3 flex gap-3 rounded-md bg-white/90 px-2.5 py-1.5 text-[11px] text-slate-700 shadow-sm dark:bg-slate-900/90 dark:text-slate-300">
        {(["green", "amber", "red"] as const).map((t) => (
          <span key={t} className="flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-full" style={{ background: TIER[t].hex }} />
            {TIER[t].label}
          </span>
        ))}
      </div>
    </div>
  );
}
