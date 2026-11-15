// Copyright OpenCuria contributors. AGPL-3.0-only.

#import <Expo/ExpoReactNativeFactory.h>

NS_ASSUME_NONNULL_BEGIN

@class EXAppContext;

/**
 React Native factory of one installed app. Like Expo's factory it creates the
 Expo `AppContext` when the runtime starts, but takes it from `makeAppContext`
 so the context carries the app's sandbox before native modules register,
 reports fatal JavaScript errors instead of letting React Native abort the
 whole OpenCuria process, and leaves React Native's process-wide provider of
 third-party Fabric components with OpenCuria's own factory.
 */
NS_SWIFT_NAME(AppRuntimeFactory)
@interface OCAppRuntimeFactory : EXReactNativeFactory

/// Called on the JS thread for the instance's context, before modules register.
@property (nonatomic, copy, nullable) EXAppContext *_Nonnull (^makeAppContext)(void);

/// Called with the message of a fatal JavaScript error.
@property (nonatomic, copy, nullable) void (^onFatalError)(NSString *message);

/// Ends the React instance; dropping its host invalidates it (RCTHost dealloc).
/// Swift cannot set `rootViewFactory.reactHost`: `RCTHost` is C++-only there.
- (void)invalidate;

@end

NS_ASSUME_NONNULL_END
