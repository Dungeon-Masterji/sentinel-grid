"use client";

import Map, { Layer, Source } from "react-map-gl/maplibre";
import "maplibre-gl/dist/maplibre-gl.css";
import { STYLE } from "@/components/sites-map";

/** Affected H3 hex, outlined and filled, centred on the cell. */
export default function AlertHexMap({
  footprint,
  color,
}: {
  footprint: GeoJSON.Feature<GeoJSON.Polygon>;
  color: string;
}) {
  const ring = footprint.geometry.coordinates[0];
  const lon = ring.reduce((s, p) => s + p[0], 0) / ring.length;
  const lat = ring.reduce((s, p) => s + p[1], 0) / ring.length;

  return (
    <div className="h-56 w-full overflow-hidden rounded-lg ring-1 ring-foreground/10">
      <Map
        initialViewState={{ latitude: lat, longitude: lon, zoom: 10.5 }}
        mapStyle={STYLE}
        style={{ width: "100%", height: "100%" }}
        attributionControl={{ compact: true }}
      >
        <Source id="hex" type="geojson" data={footprint}>
          <Layer id="hex-fill" type="fill" paint={{ "fill-color": color, "fill-opacity": 0.28 }} />
          <Layer id="hex-line" type="line" paint={{ "line-color": color, "line-width": 2 }} />
        </Source>
      </Map>
    </div>
  );
}
