require 'json'

package = JSON.parse(File.read(File.join(__dir__, 'package.json')))

Pod::Spec.new do |s|
  s.name = 'OpenCuriaPush'
  s.version = package['version']
  s.summary = 'End-to-end encrypted push keys for the OpenCuria mobile app.'
  s.description = 'Keychain-held RFC 8291 keys shared with the app extensions.'
  s.homepage = 'https://github.com/opencuria/OpenCuria'
  s.license = 'AGPL-3.0-only'
  s.author = 'OpenCuria contributors'
  s.platforms = { :ios => '16.1' }
  s.source = { :path => '.' }
  # extension/ belongs to the notification service extension target only.
  s.source_files = 'ios/**/*.swift'
  s.frameworks = 'CryptoKit', 'Security'
  s.swift_version = '5.9'
  s.dependency 'ExpoModulesCore'
end
