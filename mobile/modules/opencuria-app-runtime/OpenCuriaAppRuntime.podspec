require 'json'

package = JSON.parse(File.read(File.join(__dir__, 'package.json')))

Pod::Spec.new do |s|
  s.name = 'OpenCuriaAppRuntime'
  s.version = package['version']
  s.summary = 'Runs apps agents publish inside the OpenCuria mobile app.'
  s.description = 'A separate React Native instance per installed app, with its own sandbox, like Expo Go experiences.'
  s.homepage = 'https://github.com/opencuria/OpenCuria'
  s.license = 'AGPL-3.0-only'
  s.author = 'OpenCuria contributors'
  s.platforms = { :ios => '16.1' }
  s.source = { :path => '.' }
  s.source_files = 'ios/**/*.{h,mm,swift}'
  s.swift_version = '5.9'
  s.frameworks = 'UIKit', 'Security'
  s.dependency 'ExpoModulesCore'
  s.dependency 'Expo'
  s.dependency 'React-RCTAppDelegate'
  s.dependency 'ReactAppDependencyProvider'
  # Networking hooks that route app backends (RCTHTTPRequestHandler.h,
  # RCTWebSocketModule.h).
  s.dependency 'React-RCTNetwork'
  s.dependency 'React-CoreModules'
  s.pod_target_xcconfig = {
    'DEFINES_MODULE' => 'YES',
    'CLANG_CXX_LANGUAGE_STANDARD' => 'c++20',
  }
  install_modules_dependencies(s)
end
