require 'json'

package = JSON.parse(File.read(File.join(__dir__, 'package.json')))

Pod::Spec.new do |s|
  s.name = 'OpenCuriaPdf'
  s.version = package['version']
  s.summary = 'Native PDF view for the OpenCuria mobile app.'
  s.description = 'PDFKit document view bridged to React Native.'
  s.homepage = 'https://github.com/opencuria/OpenCuria'
  s.license = 'AGPL-3.0-only'
  s.author = 'OpenCuria contributors'
  s.platforms = { :ios => '16.1' }
  s.source = { :path => '.' }
  s.source_files = 'ios/**/*.swift'
  s.frameworks = 'PDFKit', 'UIKit'
  s.swift_version = '5.9'
  s.dependency 'ExpoModulesCore'
end
