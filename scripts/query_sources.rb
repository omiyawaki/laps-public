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

def fetch_json(url, params)
  uri = URI(url)
  uri.query = URI.encode_www_form(params)

  response = Net::HTTP.start(
    uri.host,
    uri.port,
    use_ssl: uri.scheme == "https",
    read_timeout: 60,
    open_timeout: 20
  ) do |http|
    request = Net::HTTP::Get.new(uri)
    request["User-Agent"] = "loop-measurement/0.1"
    http.request(request)
  end

  unless response.is_a?(Net::HTTPSuccess)
    raise "GET #{uri} failed: #{response.code} #{response.message}"
  end

  parsed = JSON.parse(response.body)
  raise "ArcGIS error from #{url}: #{parsed["error"].inspect}" if parsed["error"]

  parsed
end

def arcgis_point_query(layer_or_service_url, lon, lat, extra_params = {})
  params = {
    f: "json",
    where: "1=1",
    geometry: { x: lon, y: lat, spatialReference: { wkid: 4326 } }.to_json,
    geometryType: "esriGeometryPoint",
    inSR: "4326",
    spatialRel: "esriSpatialRelIntersects",
    outFields: "*",
    returnGeometry: "false"
  }.merge(extra_params)

  fetch_json(query_url(layer_or_service_url), params)
end

def attributes(json)
  json.fetch("features", []).map { |feature| feature.fetch("attributes", {}) }
end

def pick(attrs, keys)
  keys.each_with_object({}) do |key, kept|
    kept[key] = attrs[key] if attrs.key?(key)
  end
end

def relevant_street?(attrs)
  text = [
    attrs["CompleteStreetName"],
    attrs["Label"],
    attrs["StreetName"]
  ].compact.join(" ")

  text.match?(/loop|crossa|crossb/i)
end

options = {
  config: File.join(PROJECT_ROOT, "config", "site.yaml"),
  output: nil,
  street_radius_m: 500
}

OptionParser.new do |parser|
  parser.banner = "Usage: scripts/query_sources.rb [options]"
  parser.on("--config PATH", "Site config YAML") { |value| options[:config] = value }
  parser.on("--output PATH", "Write JSON output to PATH") { |value| options[:output] = value }
  parser.on("--street-radius-m METERS", Integer, "Street query radius") do |value|
    options[:street_radius_m] = value
  end
end.parse!

config = YAML.load_file(options[:config])
lat = config.fetch("site").fetch("center").fetch("latitude")
lon = config.fetch("site").fetch("center").fetch("longitude")
sources = config.fetch("sources")

ortho = sources.fetch("nys_orthoimagery")
lidar = sources.fetch("nys_lidar")
streets = sources.fetch("nys_streets")

ortho_latest = attributes(arcgis_point_query(ortho.fetch("latest_index"), lon, lat))
ortho_all_years = attributes(arcgis_point_query(ortho.fetch("all_years_index"), lon, lat))
lidar_latest = attributes(arcgis_point_query(lidar.fetch("latest_collections_layer"), lon, lat))

street_query = arcgis_point_query(
  streets.fetch("feature_layer"),
  lon,
  lat,
  {
    distance: options[:street_radius_m].to_s,
    units: "esriSRUnit_Meter",
    outFields: [
      "OBJECTID",
      "CompleteStreetName",
      "StreetName",
      "PostDirectional",
      "Label",
      "LeftCountyName",
      "RightCountyName",
      "GeometryAccuracy",
      "GeometrySource",
      "NameSource",
      "Status",
      "OneWay",
      "Shape__Length"
    ].join(",")
  }
)
street_features = attributes(street_query)
relevant_streets = street_features.select { |attrs| relevant_street?(attrs) }

result = {
  generated_at_utc: Time.now.utc.iso8601,
  config: File.expand_path(options[:config]),
  aoi: {
    latitude: lat,
    longitude: lon,
    street_radius_m: options[:street_radius_m]
  },
  orthoimagery_latest: {
    query_url: query_url(ortho.fetch("latest_index")),
    count: ortho_latest.length,
    features: ortho_latest
  },
  orthoimagery_all_years: {
    query_url: query_url(ortho.fetch("all_years_index")),
    count: ortho_all_years.length,
    features: ortho_all_years
  },
  lidar_latest_collections: {
    query_url: query_url(lidar.fetch("latest_collections_layer")),
    count: lidar_latest.length,
    features: lidar_latest
  },
  nys_streets_relevant: {
    query_url: query_url(streets.fetch("feature_layer")),
    total_nearby_count: street_features.length,
    relevant_count: relevant_streets.length,
    features: relevant_streets.map do |attrs|
      pick(
        attrs,
        [
          "OBJECTID",
          "CompleteStreetName",
          "Label",
          "LeftCountyName",
          "RightCountyName",
          "GeometryAccuracy",
          "GeometrySource",
          "NameSource",
          "Status",
          "OneWay",
          "Shape__Length"
        ]
      )
    end
  }
}

json = JSON.pretty_generate(result)

if options[:output]
  output_path = File.expand_path(options[:output])
  FileUtils.mkdir_p(File.dirname(output_path))
  File.write(output_path, "#{json}\n")
else
  puts json
end
