// Copyright OpenCuria contributors. AGPL-3.0-only.

#import "AppRuntimeFactory.h"

#import <ExpoModulesCore/EXHostWrapper.h>
#import <ExpoModulesCore/EXReactSchedulerDispatch.h>
#import <React/RCTComponentViewFactory.h>
#import <React/RCTDataRequestHandler.h>
#import <React/RCTExceptionsManager.h>
#import <React/RCTFileRequestHandler.h>
#import <React/RCTHTTPRequestHandler.h>
#import <React/RCTInvalidating.h>
#import <React/RCTNetworking.h>
#import <ReactCommon/RCTHost.h>
#import <react/renderer/runtimescheduler/RuntimeSchedulerBinding.h>

#if __has_include(<ExpoModulesCore/ExpoModulesCore-Swift.h>)
#import <ExpoModulesCore/ExpoModulesCore-Swift.h>
#else
#import "ExpoModulesCore-Swift.h"
#endif

// RCTExceptionsManager.mm sends every fatal report (`reportException` with
// `isFatal`, `reportFatalException`) through this method, which ends in
// RCTFatal in release builds. Check it still exists when upgrading React Native.
@interface RCTExceptionsManager (OCFatalReport)
- (void)reportFatal:(NSString *)message
              stack:(NSArray<NSDictionary *> *)stack
        exceptionId:(double)exceptionId
    extraDataAsJSON:(nullable NSString *)extraDataAsJSON;
@end

// RCTReactNativeFactory implements RCTTurboModuleManagerDelegate, but adopts it
// only in its class extension (RCTReactNativeFactory.mm), so the header does not
// declare the method this factory forwards to via super. Check it still exists
// when upgrading React Native.
@interface RCTReactNativeFactory (OCTurboModuleManagerDelegate)
- (id<RCTTurboModule>)getModuleInstanceFromClass:(Class)moduleClass;
@end

/**
 The app instance's ExceptionsManager. Once the bundle has run, React Native
 reports uncaught JavaScript errors (e.g. in a render) through this module
 instead of the host delegate, and RCTFatal would end the whole OpenCuria
 process. Fatal reports go to the app's error screen instead.
 */
@interface OCAppExceptionsManager : RCTExceptionsManager
- (instancetype)initWithFatalHandler:(void (^)(NSString *message))onFatal;
@end

@implementation OCAppExceptionsManager {
  void (^_onFatal)(NSString *message);
}

// Not RCT_EXPORT_MODULE: its +load would register this class process-wide.
+ (NSString *)moduleName
{
  return @"ExceptionsManager";
}

- (instancetype)initWithFatalHandler:(void (^)(NSString *message))onFatal
{
  if (self = [super init]) {
    _onFatal = [onFatal copy];
  }
  return self;
}

- (void)reportFatal:(NSString *)message
              stack:(NSArray<NSDictionary *> *)stack
        exceptionId:(double)exceptionId
    extraDataAsJSON:(nullable NSString *)extraDataAsJSON
{
  _onFatal(message ?: @"");
}

@end

/**
 Loads `file:` URLs for the app instance's networking module, which fetch,
 XMLHttpRequest, request bodies with files and the image loader all use. It
 claims every `file:` URL ahead of other handlers and loads only those the
 app may read, through React Native's own file handler.
 */
@interface OCAppFileRequestHandler : NSObject <RCTURLRequestHandler, RCTInvalidating>
- (instancetype)initWithCanRead:(nullable BOOL (^)(NSURL *url))canRead;
@end

@implementation OCAppFileRequestHandler {
  BOOL (^_canRead)(NSURL *url);
  RCTFileRequestHandler *_files;
  NSOperationQueue *_denials;
}

// Not RCT_EXPORT_MODULE: only the app's networking module uses it.
+ (NSString *)moduleName
{
  return @"OCAppFileRequestHandler";
}

- (instancetype)initWithCanRead:(nullable BOOL (^)(NSURL *url))canRead
{
  if (self = [super init]) {
    _canRead = [canRead copy];
    _files = [RCTFileRequestHandler new];
    _denials = [NSOperationQueue new];
  }
  return self;
}

- (BOOL)canHandleRequest:(NSURLRequest *)request
{
  return [request.URL.scheme caseInsensitiveCompare:@"file"] == NSOrderedSame;
}

- (float)handlerPriority
{
  return 1000;
}

- (id)sendRequest:(NSURLRequest *)request withDelegate:(id<RCTURLRequestDelegate>)delegate
{
  if (_canRead != nil && _canRead(request.URL)) {
    return [_files sendRequest:request withDelegate:delegate];
  }
  // Reported asynchronously, like every other request's completion.
  NSBlockOperation *operation = [NSBlockOperation new];
  __weak NSBlockOperation *weakOperation = operation;
  [operation addExecutionBlock:^{
    NSBlockOperation *strongOperation = weakOperation;
    if (strongOperation == nil || strongOperation.isCancelled) {
      return;
    }
    NSError *error = [NSError errorWithDomain:NSCocoaErrorDomain
                                         code:NSFileReadNoPermissionError
                                     userInfo:@{NSLocalizedDescriptionKey : @"This app may not read this file."}];
    [delegate URLRequest:strongOperation didCompleteWithError:error];
  }];
  [_denials addOperation:operation];
  return operation;
}

- (void)cancelRequest:(id)requestToken
{
  if ([requestToken isKindOfClass:NSOperation.class]) {
    [(NSOperation *)requestToken cancel];
  }
}

- (void)invalidate
{
  [_files invalidate];
  [_denials cancelAllOperations];
}

@end

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
  if (self.registerModules) {
    self.registerModules(appContext);
  } else {
    [appContext registerNativeModules];
  }
}

// [JS thread] The app's own ExceptionsManager and networking; every other module as usual.
- (id<RCTTurboModule>)getModuleInstanceFromClass:(Class)moduleClass
{
  if (moduleClass == [RCTExceptionsManager class]) {
    __weak OCAppRuntimeFactory *weakSelf = self;
    return (id<RCTTurboModule>)[[OCAppExceptionsManager alloc] initWithFatalHandler:^(NSString *message) {
      [weakSelf reportFatalError:message];
    }];
  }
  if (moduleClass == [RCTNetworking class]) {
    return [self makeNetworking];
  }
  return [super getModuleInstanceFromClass:moduleClass];
}

// React Native's networking module (RCTAppSetupDefaultModuleFromClass in
// RCTAppSetupUtils.mm) with OCAppFileRequestHandler in place of
// RCTFileRequestHandler. Check it still matches when upgrading React Native.
- (id<RCTTurboModule>)makeNetworking
{
  BOOL (^canReadFile)(NSURL *) = self.canReadFile;
  NSArray<NSString *> *libraryHandlers = self.delegate.dependencyProvider.URLRequestHandlerClassNames ?: @[];
  return (id<RCTTurboModule>)[[RCTNetworking alloc]
      initWithHandlersProvider:^NSArray<id<RCTURLRequestHandler>> *(RCTModuleRegistry *moduleRegistry) {
        NSMutableArray<id<RCTURLRequestHandler>> *handlers = [@[
          [RCTHTTPRequestHandler new],
          [RCTDataRequestHandler new],
          [[OCAppFileRequestHandler alloc] initWithCanRead:canReadFile],
          [moduleRegistry moduleForName:"BlobModule"],
        ] mutableCopy];
        for (NSString *className in libraryHandlers) {
          id handler = [moduleRegistry moduleForName:[className UTF8String]];
          if ([handler conformsToProtocol:@protocol(RCTURLRequestHandler)]) {
            [handlers addObject:handler];
          }
        }
        return handlers;
      }];
}

// Implementing this keeps fatal errors away from RCTFatal (RCTHost.mm) while
// the bundle loads; later ones reach OCAppExceptionsManager.
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
  [self reportFatalError:originalMessage ?: message];
}
#pragma clang diagnostic pop

// [Any thread]
- (void)reportFatalError:(NSString *)message
{
  dispatch_async(dispatch_get_main_queue(), ^{
    // Read on the main thread, where it is set; calling a nil block crashes.
    void (^onFatalError)(NSString *) = self.onFatalError;
    if (onFatalError) {
      onFatalError(message);
    }
  });
}

- (void)invalidate
{
  self.rootViewFactory.reactHost = nil;
}

@end
