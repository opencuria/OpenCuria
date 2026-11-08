// Copyright OpenCuria contributors. AGPL-3.0-only.

#import "AppRuntimeFactory.h"

#import <ExpoModulesCore/EXHostWrapper.h>
#import <ExpoModulesCore/EXReactSchedulerDispatch.h>
#import <React/RCTComponentViewFactory.h>
#import <ReactCommon/RCTHost.h>
#import <react/renderer/runtimescheduler/RuntimeSchedulerBinding.h>

#if __has_include(<ExpoModulesCore/ExpoModulesCore-Swift.h>)
#import <ExpoModulesCore/ExpoModulesCore-Swift.h>
#else
#import "ExpoModulesCore-Swift.h"
#endif

@implementation OCAppRuntimeFactory {
  // The context lives as long as this factory's single React instance.
  EXAppContext *_appContext;
}

// [Main thread] Both initializers of RCTReactNativeFactory end up here.
- (instancetype)initWithDelegate:(id<RCTReactNativeFactoryDelegate>)delegate releaseLevel:(RCTReleaseLevel)releaseLevel
{
  // RCTReactNativeFactory makes every new factory the provider of third-party
  // Fabric components, held weakly by the process-wide RCTComponentViewFactory.
  // This factory ends with its app; afterwards every third-party component that
  // OpenCuria renders for the first time would become an <UnimplementedView>,
  // and the factory caches that until the process ends. OpenCuria's factory
  // lives as long as the process and resolves the same components (both use the
  // generated RCTAppDependencyProvider), so it stays the provider for both
  // instances. A provider of another app instance is not kept: it ends too.
  RCTComponentViewFactory *componentViewFactory = [RCTComponentViewFactory currentComponentViewFactory];
  id<RCTComponentViewFactoryComponentProvider> provider = componentViewFactory.thirdPartyFabricComponentsProvider;
  if (self = [super initWithDelegate:delegate releaseLevel:releaseLevel]) {
    if (provider != nil && ![provider isKindOfClass:[OCAppRuntimeFactory class]]) {
      componentViewFactory.thirdPartyFabricComponentsProvider = provider;
    }
  }
  return self;
}

// [JS thread] Mirrors EXReactNativeFactory, with the sandbox applied first.
- (void)host:(nonnull RCTHost *)host didInitializeRuntime:(facebook::jsi::Runtime &)runtime
{
  EXAppContext *appContext = self.makeAppContext ? self.makeAppContext() : [[EXAppContext alloc] init];
  _appContext = appContext;

  auto binding = facebook::react::RuntimeSchedulerBinding::getBinding(runtime);
  auto scheduler = binding ? binding->getRuntimeScheduler() : nullptr;
  void *schedulerHandle = expo::createReactSchedulerHandle(scheduler);
  [appContext setRuntime:&runtime
               scheduler:schedulerHandle
                dispatch:schedulerHandle ? reinterpret_cast<const void *>(&expo::dispatchOnReactScheduler) : nullptr];
  [appContext setHostWrapper:[[EXHostWrapper alloc] initWithHost:host]];
  [appContext registerNativeModules];
}

// Implementing this keeps fatal errors away from RCTFatal (RCTHost.mm).
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wdeprecated-implementations"
- (void)host:(RCTHost *)host
    didReceiveJSErrorStack:(NSArray<NSDictionary<NSString *, id> *> *)stack
                   message:(NSString *)message
           originalMessage:(NSString *_Nullable)originalMessage
                      name:(NSString *_Nullable)name
            componentStack:(NSString *_Nullable)componentStack
               exceptionId:(NSUInteger)exceptionId
                   isFatal:(BOOL)isFatal
                 extraData:(NSDictionary<NSString *, id> *)extraData
{
  if (!isFatal) {
    return;
  }
  NSString *text = originalMessage ?: message;
  dispatch_async(dispatch_get_main_queue(), ^{
    // Read on the main thread, where it is set; calling a nil block crashes.
    void (^onFatalError)(NSString *) = self.onFatalError;
    if (onFatalError) {
      onFatalError(text);
    }
  });
}
#pragma clang diagnostic pop

- (void)invalidate
{
  self.rootViewFactory.reactHost = nil;
}

@end
