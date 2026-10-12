package com.bookmap.orderflow.hello;

import velox.api.layer1.annotations.Layer1ApiVersion;
import velox.api.layer1.annotations.Layer1ApiVersionValue;
import velox.api.layer1.annotations.Layer1SimpleAttachable;
import velox.api.layer1.annotations.Layer1StrategyName;
import velox.api.layer1.common.Log;
import velox.api.layer1.data.InstrumentInfo;
import velox.api.layer1.simplified.Api;
import velox.api.layer1.simplified.CustomModule;
import velox.api.layer1.simplified.InitialState;

/**
 * Minimal load-test module. Enable once to confirm the JAR loads in Bookmap,
 * then disable/remove it.
 */
@Layer1SimpleAttachable
@Layer1StrategyName("Orderflow Hello")
@Layer1ApiVersion(Layer1ApiVersionValue.VERSION2)
public class HelloModule implements CustomModule {

    @Override
    public void initialize(String alias, InstrumentInfo info, Api api, InitialState initialState) {
        Log.info("Orderflow Hello loaded on " + alias + " (" + info.fullName + ")");
    }

    @Override
    public void stop() {
        Log.info("Orderflow Hello stopped");
    }
}
