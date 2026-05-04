#!/usr/bin/env ruby
# frozen_string_literal: true

require "fileutils"
require "json"
require "net/http"
require "optparse"
require "time"
require "uri"
require "yaml"

PROJECT_ROOT = File.expand_path("..", __dir__)
WEB_MERCATOR_RADIUS_M = 6_378_137.0
MAX_WEB_MERCATOR_LAT = 85.05112878

def lonlat_to_web_mercator(lon, lat)
  clamped_lat = [[lat, MAX_WEB_MERCATOR_LAT].min, -MAX_WEB_MERCATOR_LAT].max
  lon_rad = lon * Math::PI / 180.0
  lat_rad = clamped_lat * Math::PI / 180.0

  x = WEB_MERCATOR_RADIUS_M * lon_rad
  y = WEB_MERCATOR_RADIUS_M * Math.log(Math.tan((Math::PI / 4.0) + (lat_rad / 2.0)))
  [x, y]
end

def fetch_bytes(url, params)
  uri = URI(url)
  uri.query = URI.encode_www_form(params)

  response = Net::HTTP.start(
    uri.host,
    uri.port,
    use_ssl: uri.scheme == "https",
    read_timeout: 120,
    open_timeout: 20
  ) do |http|
    request = Net::HTTP::Get.new(uri)
    request["User-Agent"] = "loop-measurement/0.1"
    http.request(request)
  end

  unless response.is_a?(Net::HTTPSuccess)
    raise "GET #{uri} failed: #{response.code} #{response.message}"
  end

  response.body
end

def fetch_json(url, params)
  JSON.parse(fetch_bytes(url, params))
end

def query_url(layer_or_service_url)
  base = layer_or_service_url.sub(%r{/*\z}, "")
  if base.match?(%r{/FeatureServer/\d+\z})
    "#{base}/query"
  elsif base.match?(%r{/FeatureServer\z})
    "#{base}/0/query"
  else
    "#{base}/query"
  end
end

def arcgis_point_query(layer_or_service_url, lon, lat, distance_m:, out_fields:, return_geometry:, out_sr: nil)
  params = {
    f: "json",
    where: "1=1",
    geometry: { x: lon, y: lat, spatialReference: { wkid: 4326 } }.to_json,
    geometryType: "esriGeometryPoint",
    inSR: "4326",
    spatialRel: "esriSpatialRelIntersects",
    distance: distance_m.to_s,
    units: "esriSRUnit_Meter",
    outFields: out_fields,
    returnGeometry: return_geometry ? "true" : "false"
  }
  params[:outSR] = out_sr.to_s if out_sr

  parsed = fetch_json(query_url(layer_or_service_url), params)
  raise "ArcGIS error: #{parsed["error"].inspect}" if parsed["error"]

  parsed
end

def selected_street?(feature, selected_labels, selected_objectids)
  attrs = feature.fetch("attributes", {})
  objectid = attrs["OBJECTID"].to_i
  return selected_objectids.include?(objectid) unless selected_objectids.empty?

  label = attrs["Label"].to_s
  complete_name = attrs["CompleteStreetName"].to_s

  selected_labels.include?(label) ||
    selected_labels.any? { |selected| complete_name.casecmp?(selected.sub(" N", " North").sub(" S", " South")) }
end

def geometry_points(feature)
  geometry = feature.fetch("geometry", {})
  return geometry.fetch("paths").flatten(1) if geometry["paths"]

  []
end

def bbox_from_points(points)
  xs = points.map { |point| point[0] }
  ys = points.map { |point| point[1] }
  [xs.min, ys.min, xs.max, ys.max]
end

def expanded_bbox(bbox, margin_m)
  [
    bbox[0] - margin_m,
    bbox[1] - margin_m,
    bbox[2] + margin_m,
    bbox[3] + margin_m
  ]
end

def output_size_for_bbox(bbox, max_size_px, target_m_per_px)
  width_m = bbox[2] - bbox[0]
  height_m = bbox[3] - bbox[1]

  if target_m_per_px&.positive?
    width_px = [(width_m / target_m_per_px).ceil, 1].max
    height_px = [(height_m / target_m_per_px).ceil, 1].max
  elsif width_m >= height_m
    width_px = max_size_px
    height_px = [(max_size_px * height_m / width_m).round, 1].max
  else
    height_px = max_size_px
    width_px = [(max_size_px * width_m / height_m).round, 1].max
  end

  largest = [width_px, height_px].max
  if largest > max_size_px
    scale = max_size_px.to_f / largest
    width_px = [(width_px * scale).round, 1].max
    height_px = [(height_px * scale).round, 1].max
  end

  [width_px, height_px]
end

def loop_streets_bbox(config, lon, lat)
  crop_config = config.fetch("imagery_crop")
  street_layer = config.fetch("sources").fetch("nys_streets").fetch("feature_layer")
  selected_labels = crop_config.fetch("selected_street_labels")
  selected_objectids = crop_config.fetch("selected_street_objectids", []).map(&:to_i)

  street_json = arcgis_point_query(
    street_layer,
    lon,
    lat,
    distance_m: crop_config.fetch("street_query_radius_m"),
    out_fields: [
      "OBJECTID",
      "CompleteStreetName",
      "Label",
      "GeometrySource",
      "Shape__Length"
    ].join(","),
    return_geometry: true,
    out_sr: 3857
  )

  selected = street_json.fetch("features", []).select do |feature|
    selected_street?(feature, selected_labels, selected_objectids)
  end

  found_objectids = selected.map { |feature| feature.fetch("attributes", {})["OBJECTID"].to_i }
  missing_objectids = selected_objectids - found_objectids
  unless missing_objectids.empty?
    raise "Configured Loop street OBJECTIDs were not found: #{missing_objectids.join(", ")}"
  end

  points = selected.flat_map { |feature| geometry_points(feature) }
  raise "No Loop street geometry found for crop derivation" if points.empty?

  {
    bbox: expanded_bbox(bbox_from_points(points), crop_config.fetch("margin_m").to_f),
    features: selected.map { |feature| feature.fetch("attributes", {}) },
    query_count: street_json.fetch("features", []).length
  }
end

options = {
  config: File.join(PROJECT_ROOT, "config", "site.yaml"),
  output: File.join(PROJECT_ROOT, "build", "ortho-2024-loop-tight.png"),
  metadata_output: nil,
  radius_m: 350,
  max_size_px: nil,
  crop_mode: nil
}

OptionParser.new do |parser|
  parser.banner = "Usage: scripts/export_ortho.rb [options]"
  parser.on("--config PATH", "Site config YAML") { |value| options[:config] = value }
  parser.on("--output PATH", "Output PNG path") { |value| options[:output] = value }
  parser.on("--metadata-output PATH", "Output metadata JSON path") { |value| options[:metadata_output] = value }
  parser.on("--crop-mode MODE", "site-radius or nys-streets-loop-bbox") { |value| options[:crop_mode] = value }
  parser.on("--radius-m METERS", Integer, "Half-width for site-radius crop") { |value| options[:radius_m] = value }
  parser.on("--max-size-px PIXELS", Integer, "Maximum output image dimension") { |value| options[:max_size_px] = value }
end.parse!

config = YAML.load_file(options[:config])
lat = config.fetch("site").fetch("center").fetch("latitude")
lon = config.fetch("site").fetch("center").fetch("longitude")
service_url = config.fetch("sources").fetch("nys_orthoimagery").fetch("service_2024")
crop_config = config.fetch("imagery_crop", {})
crop_mode = options[:crop_mode] || crop_config.fetch("strategy", "site_radius").tr("_", "-")
max_size_px = options[:max_size_px] || crop_config.fetch("max_size_px", 2400)
target_m_per_px = crop_config["target_m_per_px"]&.to_f

crop_metadata = {}

bbox =
  case crop_mode
  when "site-radius"
    center_x, center_y = lonlat_to_web_mercator(lon, lat)
    radius = options[:radius_m].to_f
    crop_metadata[:radius_m] = radius
    [
      center_x - radius,
      center_y - radius,
      center_x + radius,
      center_y + radius
    ]
  when "nys-streets-loop-bbox"
    derived = loop_streets_bbox(config, lon, lat)
    crop_metadata[:street_query_count] = derived[:query_count]
    crop_metadata[:selected_street_features] = derived[:features]
    derived[:bbox]
  else
    raise "Unsupported crop mode: #{crop_mode}"
  end

width_px, height_px = output_size_for_bbox(bbox, max_size_px, target_m_per_px)

export_url = "#{service_url.sub(%r{/*\z}, "")}/export"
params = {
  f: "image",
  bbox: bbox.join(","),
  bboxSR: "3857",
  imageSR: "3857",
  size: "#{width_px},#{height_px}",
  format: "png32",
  transparent: "false"
}

png = fetch_bytes(export_url, params)
output_path = File.expand_path(options[:output])
FileUtils.mkdir_p(File.dirname(output_path))
File.binwrite(output_path, png)

metadata_path = File.expand_path(options[:metadata_output] || output_path.sub(/\.[^.]+\z/, ".json"))
FileUtils.mkdir_p(File.dirname(metadata_path))
metadata = {
  generated_at_utc: Time.now.utc.iso8601,
  config: File.expand_path(options[:config]),
  crop_mode: crop_mode,
  source_service: service_url,
  bbox_epsg3857: bbox,
  width_m: bbox[2] - bbox[0],
  height_m: bbox[3] - bbox[1],
  image_size_px: [width_px, height_px],
  meters_per_pixel: {
    x: (bbox[2] - bbox[0]) / width_px.to_f,
    y: (bbox[3] - bbox[1]) / height_px.to_f
  },
  crop_metadata: crop_metadata
}
File.write(metadata_path, "#{JSON.pretty_generate(metadata)}\n")

puts "wrote #{output_path}"
puts "wrote #{metadata_path}"
puts "bbox EPSG:3857 #{bbox.join(",")}"
puts "size #{width_px}x#{height_px}px"
puts "approx #{format("%.3f", metadata[:meters_per_pixel][:x])} m/px"
